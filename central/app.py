"""Aplicação web (FastAPI + Jinja2, páginas renderizadas no servidor).

Rodar:  uvicorn central.app:app --reload --port 8000
"""
from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import activities as acts
from . import ask
from . import busca
from . import drive_auth
from . import transcribe
from . import suggestions as sugg
from . import views
from .authority import ROLE_LABELS
from .config import get_settings
from .db import connect, get_setting, init_db, now_iso
from .issues import ISSUE_LABELS, open_issues, resolve_issue
from .sources import DriveSource, LocalSource, SourceError
from .sync import run_sync

log = logging.getLogger("central")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

settings = get_settings()
BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
from .db import DEMO_MEMBERS  # noqa: E402

_NAMES = {m[0]: m[1] for m in DEMO_MEMBERS}


def _join_names(ids) -> str:
    """'Ana', 'Ana e Davi', 'Ana, Bruno e Davi'."""
    names = [_NAMES.get(i, i) for i in (ids or [])]
    return " e ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " e " + names[-1]


templates.env.filters["nomes"] = _join_names
templates.env.filters["humano"] = views.humano
templates.env.filters["humano_linhas"] = views.humano_linhas
templates.env.filters["field_label"] = lambda k: acts.FIELD_LABELS.get(k, k).lower()
templates.env.filters["juntar"] = lambda xs: (" e ".join(xs) if len(xs) <= 2 else ", ".join(xs[:-1]) + " e " + xs[-1])
templates.env.filters["frase"] = lambda t: (t[:1].upper() + t[1:]) if t else t
def _doc_kind(s) -> str:
    """Tipo do documento na tela; arquivos não lidos dizem o que são ("Imagem", "PDF escaneado")."""
    if s["sync_status"] == "nao_suportado":
        return transcribe.kind_label(s) or "Formato não lido"
    return ROLE_LABELS.get(s["role"], "Tipo não reconhecido")


templates.env.globals.update(
    doc_kind=_doc_kind,
    NAMES=_NAMES, date_parts=views.date_parts,
    humano=views.humano, sheet_ref=views.sheet_ref, doc_meta_items=views.doc_meta_items, doc_status=views.doc_status,
    fmt_date=views.fmt_date, fmt_ts=views.fmt_ts, fmt_when=views.fmt_when, due_info=views.due_info, ROLE_LABELS=ROLE_LABELS,
    FIELD_LABELS=acts.FIELD_LABELS, ISSUE_LABELS=ISSUE_LABELS, SUG_STATUS=sugg.SUG_STATUS, STATUSES=acts.STATUSES,
    STALE_DAYS=settings.stale_days,
    # dias sem novidade (ou None): usado no selo "Sem novidade há N dias" dos cartões
    sem_novidade=lambda a: acts.days_without_news(a, views.today(), settings.stale_days),
)

_sync_lock = threading.Lock()
_stop = threading.Event()


def db():
    conn = connect(settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sincronização (automática + manual)
# ---------------------------------------------------------------------------
def make_source(conn):
    if settings.source_mode == "local":
        return LocalSource(settings.local_folder)
    if not settings.drive_configured:
        raise drive_auth.DriveNotConnected("Drive não configurado: preencha GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET e "
                                           "DRIVE_TEST_FOLDER_ID no .env.")
    return DriveSource(drive_auth.load_credentials(conn, settings), settings.drive_folder_id)


def make_llm():
    if settings.llm_enabled:
        from .ai import GeminiLLM
        return GeminiLLM(settings.gemini_api_key, settings.gemini_model, settings.gemini_fallback_model)
    return None


def make_qa_llm():
    """IA das perguntas: modelos rápidos primeiro e sem espera entre tentativas (quem pergunta está esperando)."""
    if settings.llm_enabled:
        from .ai import GeminiLLM
        models = [m.strip() for m in settings.gemini_qa_models.split(",") if m.strip()] or [settings.gemini_model]
        return GeminiLLM(settings.gemini_api_key, models[0], ",".join(models[1:]), timeout=20.0, attempts=1)
    return None


def make_vision_llm():
    """IA da transcrição: modelos principais (qualidade de leitura), sem espera entre tentativas."""
    if settings.llm_enabled:
        from .ai import GeminiLLM
        return GeminiLLM(settings.gemini_api_key, settings.gemini_model, settings.gemini_fallback_model,
                         timeout=90.0, attempts=1)
    return None


def do_sync(trigger: str) -> dict:
    if not _sync_lock.acquire(blocking=False):
        return {"status": "ocupado", "message": "Já existe uma sincronização em andamento."}
    conn = connect(settings.database_path)
    try:
        try:
            source = make_source(conn)
        except (drive_auth.DriveNotConnected, SourceError) as e:
            conn.execute("INSERT INTO sync_runs (trigger, started_at, finished_at, status, message) "
                         "VALUES (?,?,?, 'falhou', ?)", (trigger, now_iso(), now_iso(), str(e)))
            return {"status": "falhou", "message": str(e)}
        cached = json.loads(get_setting(conn, "drive_folder") or "null")
        if hasattr(source, "folder_info") and (not cached or cached.get("id") != settings.drive_folder_id):
            try:   # nome e link da pasta conectada, para mostrar na tela (R01)
                from .db import set_setting
                set_setting(conn, "drive_folder", json.dumps(source.folder_info(), ensure_ascii=False))
            except Exception:
                log.warning("Não consegui ler o nome da pasta conectada", exc_info=True)
        return run_sync(conn, source, trigger=trigger, llm=make_llm())
    except Exception as e:  # nunca derruba o agendador
        log.exception("Falha inesperada na sincronização")
        conn.execute("INSERT INTO sync_runs (trigger, started_at, finished_at, status, message) "
                     "VALUES (?,?,?, 'falhou', ?)", (trigger, now_iso(), now_iso(), f"Erro inesperado: {e}"))
        return {"status": "falhou", "message": str(e)}
    finally:
        conn.close()
        _sync_lock.release()


def scheduler_loop():
    """Roda a cada SYNC_INTERVAL_SECONDS. Em falha, espera mais (atraso progressivo),
    limitado a 10 minutos para continuar dentro da meta de 15 minutos."""
    failures = 0
    delay = 3
    while not _stop.wait(delay):
        result = do_sync("auto")
        failures = 0 if result.get("status") in ("ok", "parcial", "ocupado") else failures + 1
        delay = settings.sync_interval if failures == 0 else min(settings.sync_interval * 2 ** failures, 600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect(settings.database_path)
    init_db(conn)
    # Rodadas que ficaram "rodando" porque o app foi encerrado no meio (ex.: Ctrl+C)
    conn.execute("UPDATE sync_runs SET status='falhou', finished_at=?, "
                 "message='Interrompida: a aplicação foi encerrada durante a sincronização.' "
                 "WHERE status='rodando'", (now_iso(),))
    conn.close()
    t = threading.Thread(target=scheduler_loop, daemon=True, name="sync-scheduler")
    t.start()
    yield
    _stop.set()


app = FastAPI(title="Central LIA", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# ---------------------------------------------------------------------------
# Utilidades de página
# ---------------------------------------------------------------------------
def flash(request: Request, message: str, kind: str = "ok"):
    request.session.setdefault("flash", []).append({"message": message, "kind": kind})


def render(request: Request, conn, template: str, status_code: int = 200, **ctx):
    member_id = request.session.get("member_id")
    member = conn.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone() if member_id else None
    last_run = conn.execute("SELECT * FROM sync_runs ORDER BY run_id DESC LIMIT 1").fetchone()
    last_ok = conn.execute("SELECT * FROM sync_runs WHERE status IN ('ok','parcial') ORDER BY run_id DESC LIMIT 1").fetchone()
    ctx.update(
        request=request, me=member, members=conn.execute("SELECT * FROM members ORDER BY display_name").fetchall(),
        last_run=last_run, last_ok=last_ok, flashes=request.session.pop("flash", []),
        n_pending=conn.execute("SELECT COUNT(*) FROM suggestions WHERE review_status='pendente'").fetchone()[0],
        n_issues=conn.execute("SELECT COUNT(*) FROM issues WHERE status='aberta'").fetchone()[0],
        source_mode=settings.source_mode, llm_enabled=settings.llm_enabled,
        sync_minutes=round(settings.sync_interval / 60, 1) if settings.sync_interval % 60 else settings.sync_interval // 60,
    )
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


def require_member(request: Request):
    return request.session.get("member_id")


def to(url: str):
    return RedirectResponse(url, status_code=303)


# ---------------------------------------------------------------------------
# Identidade de demonstração
# ---------------------------------------------------------------------------
@app.get("/entrar", response_class=HTMLResponse)
def entrar_form(request: Request, conn=Depends(db)):
    return render(request, conn, "entrar.html")


@app.post("/entrar")
def entrar(request: Request, member_id: str = Form(...), conn=Depends(db)):
    m = conn.execute("SELECT * FROM members WHERE member_id=?", (member_id,)).fetchone()
    if not m:
        flash(request, "Pessoa não encontrada. Escolha alguém da lista.", "erro")
        return to("/entrar")
    request.session["member_id"] = member_id
    # Marco para "o que mudou": a visita anterior
    row = conn.execute("SELECT * FROM member_visits WHERE member_id=?", (member_id,)).fetchone()
    prev = row["last_visit_at"] if row else None
    conn.execute("INSERT INTO member_visits (member_id, last_visit_at, prev_visit_at) VALUES (?,?,?) "
                 "ON CONFLICT(member_id) DO UPDATE SET prev_visit_at=last_visit_at, last_visit_at=excluded.last_visit_at",
                 (member_id, now_iso(), prev))
    return to("/")


@app.post("/sair")
def sair(request: Request):
    request.session.pop("member_id", None)
    return to("/entrar")


# ---------------------------------------------------------------------------
# A. Minhas atividades
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request, ordem: str = "prazo", q: str = "", prazo: str = "", situacao: str = "", novidade: int = 0,
         conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    all_mine = acts.list_activities(conn, me)
    items = busca.filter_activities(all_mine, q=q, situacao=situacao, prazo=prazo, novidade=bool(novidade),
                                    today=views.today(), stale_days=settings.stale_days, names=views.member_names(conn))
    filtro = {"q": q.strip(), "prazo": prazo, "situacao": situacao, "novidade": novidade, "ordem": ordem}
    visit = conn.execute("SELECT prev_visit_at FROM member_visits WHERE member_id=?", (me,)).fetchone()
    since = visit["prev_visit_at"] if visit and visit["prev_visit_at"] else views.since_options()["7d"]
    ch = views.changes_for_member(conn, me, since)
    recent = {"since": since, "first": not (visit and visit["prev_visit_at"]), "confirmed": len(ch["confirmed"]),
              "suggestions": len([x for x in ch["suggestions"] if x["status"] == "pendente"]),
              "docs": len(ch["new_sources"]) + len(ch["edited_sources"])}
    filtering = bool(filtro["q"] or prazo or situacao or novidade)
    if ordem == "estado":
        items.sort(key=lambda a: (a["status"] != "Bloqueada", a["due_date"] or "9999"))
    mine_pending = [s for s in sugg.list_suggestions(conn) if (s["target_activity_id"] in {a["activity_id"] for a in items})
                    or me in (s["proposed"].get("owners") or [])]
    stats = {
        "open": len(all_mine),
        "soon": sum(1 for a in all_mine if views.due_info(a["due_date"], a["status"])["kind"] in ("soon", "overdue")),
        "blocked": sum(1 for a in all_mine if a["status"] == "Bloqueada"),
        "pending_me": len(mine_pending),
        "to_review": conn.execute("SELECT COUNT(*) FROM suggestions WHERE review_status='pendente'").fetchone()[0],
    }
    stale = []
    for a in all_mine:
        n = acts.days_without_news(a, views.today(), settings.stale_days)
        if n:
            stale.append(a | {"stale_days": n})
    stale.sort(key=lambda a: -a["stale_days"])
    return render(request, conn, "minhas.html", items=items, ordem=ordem, mine_pending=mine_pending, stats=stats,
                  stale=stale, filtro=filtro, filtering=filtering, recent=recent)


@app.get("/atividades", response_class=HTMLResponse)
def todas(request: Request, q: str = "", responsavel: str = "", frente: str = "", situacao: str = "", prazo: str = "",
          novidade: int = 0, concluidas: int = 0, conn=Depends(db)):
    if concluidas and not situacao:
        situacao = "todas"   # link antigo "Com as concluídas"
    everything = acts.list_activities(conn, include_done=True)
    items = busca.filter_activities(everything, q=q, responsavel=responsavel, frente=frente, situacao=situacao,
                                    prazo=prazo, novidade=bool(novidade), today=views.today(),
                                    stale_days=settings.stale_days, names=views.member_names(conn))
    filtro = {"q": q.strip(), "responsavel": responsavel, "frente": frente, "situacao": situacao, "prazo": prazo,
              "novidade": novidade}
    n_filtros = sum(1 for k in ("responsavel", "frente", "situacao", "prazo", "novidade") if filtro[k])
    return render(request, conn, "atividades.html", items=items, filtro=filtro, n_filtros=n_filtros,
                  filtering=bool(filtro["q"] or n_filtros), fronts=busca.fronts(everything),
                  PRAZOS=busca.PRAZOS, SITUACOES=busca.SITUACOES, SEM=busca.SEM)


@app.get("/atividades/nova", response_class=HTMLResponse)
def nova_form(request: Request, conn=Depends(db)):
    if not require_member(request):
        return to("/entrar")
    return render(request, conn, "atividade_form.html", a=None, errors={}, values={})


def _form_fields(form) -> tuple[dict, dict]:
    values = {
        "title": (form.get("title") or "").strip(),
        "description": (form.get("description") or "").strip() or None,
        "front": (form.get("front") or "").strip() or None,
        "next_step": (form.get("next_step") or "").strip() or None,
        "status": form.get("status") or "A fazer",
        "due_date": (form.get("due_date") or "").strip() or None,
        "notes": (form.get("notes") or "").strip() or None,
        "owners": form.getlist("owners"),
    }
    errors = {}
    if not values["title"]:
        errors["title"] = "Informe um título."
    if values["due_date"] and not acts.valid_iso_date(values["due_date"]):
        errors["due_date"] = "Use uma data válida."
    if values["status"] not in acts.STATUSES:
        errors["status"] = "Escolha um estado da lista."
    return values, errors


@app.post("/atividades/nova")
async def nova(request: Request, conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    values, errors = _form_fields(await request.form())
    if errors:
        return render(request, conn, "atividade_form.html", a=None, errors=errors, values=values)
    new_id = acts.create_activity(conn, values, actor_id=me, creation_kind="manual", reason="Criada na interface")
    flash(request, f"Atividade “{values.get('title')}” criada no quadro ({new_id}).")
    return to(f"/atividades/{new_id}")


@app.get("/atividades/{activity_id}", response_class=HTMLResponse)
def detalhe(request: Request, activity_id: str, conn=Depends(db)):
    a = acts.snapshot(conn, activity_id)
    if not a:
        return render(request, conn, "erro.html", status_code=404, message=f"Atividade {activity_id} não encontrada.")
    row = dict(conn.execute("SELECT * FROM activities WHERE activity_id=?", (activity_id,)).fetchone())
    names = views.member_names(conn)
    row["owners"] = a["owners"]
    row["owner_names"] = [names.get(o, o) for o in a["owners"]]
    history = acts.activity_history(conn, activity_id)
    for h in history:
        h["actor"] = names.get(h["actor_id"], h["actor_id"])
        h["source"] = views.source_link(conn, h["source_file_id"])
        if h["before"] is None:
            h["changes"] = []
        else:
            h["changes"] = [(acts.FIELD_LABELS.get(k, k), views.describe_value(k, h["before"].get(k), names),
                             views.describe_value(k, h["after"].get(k), names)) for k in h["after"]]
    refs = [dict(r) | {"source": views.source_link(conn, r["file_id"])} for r in conn.execute(
        "SELECT * FROM activity_refs WHERE activity_id=? ORDER BY id", (activity_id,))]
    pend = [s for s in sugg.list_suggestions(conn) if s["target_activity_id"] == activity_id]
    # "voltar" leva para a lista de onde a pessoa veio
    ref = request.headers.get("referer") or ""
    back = ("/atividades", "Todas as atividades") if "/atividades" in ref and "/atividades/" not in ref else ("/", "Minhas atividades")
    row["pending_suggestions"] = len(pend)
    last = history[0] if history else None
    row["last_update"] = {"ts": last["ts"], "by": last["actor"]} if last else None
    row["last_movement"] = history[0]["ts"] if history else row["updated_at"]
    stale_days = acts.days_without_news(row, views.today(), settings.stale_days)
    return render(request, conn, "atividade.html", a=row, history=history, refs=refs, pend=pend, names=names,
                  back_href=back[0], back_label=back[1], stale_days=stale_days,
                  can_confirm=can_confirm(conn, require_member(request), row["owners"]))


def can_confirm(conn, member_id: str | None, owners: list[str]) -> bool:
    """Responsáveis pela atividade ou quem aprova sugestões."""
    if not member_id:
        return False
    if member_id in owners:
        return True
    m = conn.execute("SELECT can_review FROM members WHERE member_id=?", (member_id,)).fetchone()
    return bool(m and m["can_review"])


def _back_to(volta: str, fallback: str) -> str:
    return volta if volta.startswith("/") and not volta.startswith("//") else fallback


@app.post("/atividades/{activity_id}/conferir")
def conferir(request: Request, activity_id: str, resposta: str = Form(...), volta: str = Form(""), conn=Depends(db)):
    """Resposta a "Isso ainda está valendo?": continua valendo ou já terminou."""
    me = require_member(request)
    if not me:
        return to("/entrar")
    a = acts.snapshot(conn, activity_id)
    if not a:
        return render(request, conn, "erro.html", status_code=404, message=f"Atividade {activity_id} não encontrada.")
    back = _back_to(volta, f"/atividades/{activity_id}")
    if not can_confirm(conn, me, a["owners"]):
        flash(request, "Só os responsáveis pela atividade ou quem aprova sugestões podem responder.", "erro")
        return to(back)
    if resposta == "terminou":
        acts.update_activity(conn, activity_id, {"status": "Concluída"}, actor_id=me,
                             reason="Já terminou (respondeu “Isso ainda está valendo?”)")
        flash(request, f"“{a['title']}” marcada como concluída.")
    else:
        acts.confirm_still_valid(conn, activity_id, me)
        flash(request, f"Registrado: “{a['title']}” continua valendo. A Central só pergunta de novo se passar mais "
                       f"{settings.stale_days} {'dia' if settings.stale_days == 1 else 'dias'} sem novidade.")
    return to(back)


@app.get("/atividades/{activity_id}/editar", response_class=HTMLResponse)
def editar_form(request: Request, activity_id: str, conn=Depends(db)):
    if not require_member(request):
        return to("/entrar")
    a = acts.snapshot(conn, activity_id)
    return render(request, conn, "atividade_form.html", a={"activity_id": activity_id}, errors={}, values=a)


@app.post("/atividades/{activity_id}/editar")
async def editar(request: Request, activity_id: str, conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    form = await request.form()
    values, errors = _form_fields(form)
    if errors:
        return render(request, conn, "atividade_form.html", a={"activity_id": activity_id}, errors=errors, values=values)
    reason = (form.get("reason") or "").strip() or "Editada na interface"
    diff = acts.update_activity(conn, activity_id, values, actor_id=me, reason=reason)
    flash(request, (f"{len(diff)} {'campo alterado' if len(diff) == 1 else 'campos alterados'}." if diff
                    else "Nada mudou: os valores já eram esses."))
    return to(f"/atividades/{activity_id}")


@app.post("/atividades/{activity_id}/estado")
def mudar_estado(request: Request, activity_id: str, status: str = Form(...), reason: str = Form(""), conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    if not acts.snapshot(conn, activity_id):
        return render(request, conn, "erro.html", status_code=404, message=f"Atividade {activity_id} não encontrada.")
    if status not in acts.STATUSES:
        flash(request, "Situação inválida. Escolha uma das opções da lista.", "erro")
        return to(f"/atividades/{activity_id}")
    changes = {"status": status}
    if status == "Bloqueada" and reason.strip():
        changes["notes"] = reason.strip()
    acts.update_activity(conn, activity_id, changes, actor_id=me, reason=reason.strip() or f"Marcada como {status}")
    flash(request, f"Atividade marcada como {status.lower()}.")
    return to(f"/atividades/{activity_id}")


# ---------------------------------------------------------------------------
# D. Revisar sugestões
# ---------------------------------------------------------------------------
@app.get("/sugestoes", response_class=HTMLResponse)
def sugestoes(request: Request, estado: str = "pendente", conn=Depends(db)):
    items = sugg.list_suggestions(conn, None if estado == "todas" else estado)
    names = views.member_names(conn)
    titles = {r["activity_id"]: r["title"] for r in conn.execute("SELECT activity_id, title FROM activities")}
    for s in items:
        s["source"] = views.source_link(conn, s["source_file_id"])
        s["target_title"] = titles.get(s["target_activity_id"])
        s["origin"] = views.sug_origin(s)
        s["rows"] = views.sug_rows(s, names)
        s["already_txt"] = views.sug_already(s, names)
    notes = [dict(r) | {"source": views.source_link(conn, r["file_id"])} for r in conn.execute(
        "SELECT n.* FROM extraction_notes n JOIN sources s ON s.file_id = n.file_id AND s.content_hash = n.source_version "
        "WHERE n.kind IN ('hipotese','barrada_validacao','sem_mudanca','instrucao_ignorada') ORDER BY n.id DESC")]
    notes = views.left_out(notes)
    return render(request, conn, "sugestoes.html", items=items, estado=estado, names=names, notes=notes)


@app.get("/sugestoes/{sid}", response_class=HTMLResponse)
def sugestao(request: Request, sid: int, conn=Depends(db)):
    s = sugg.get(conn, sid)
    if not s:
        return render(request, conn, "erro.html", status_code=404, message="Sugestão não encontrada.")
    names = views.member_names(conn)
    s["source"] = views.source_link(conn, s["source_file_id"])
    current = acts.snapshot(conn, s["target_activity_id"]) if s["target_activity_id"] else None
    rows = []
    for k, v in s["proposed"].items():
        rows.append({"field": k, "label": acts.FIELD_LABELS.get(k, k),
                     # depois da decisão, a comparação mostra o valor de antes da sugestão (o oficial já é o novo)
                     "official": views.describe_value(k, (s["current"] or {}).get(k) if s["review_status"] != "pendente"
                                                     else current.get(k), names) if current else "—",
                     "proposed": views.describe_value(k, v, names), "raw": v})
    if s["kind"] == "create" and s["review_status"] == "pendente":
        # dado ausente: o revisor completa responsável, prazo etc. antes de aceitar
        for k in views.CREATE_FIELDS:
            if k not in s["proposed"]:
                rows.append({"field": k, "label": acts.FIELD_LABELS.get(k, k), "official": "—",
                             "proposed": "—", "raw": [] if k == "owners" else "", "missing": True})
    ver = conn.execute("SELECT content_hash FROM sources WHERE file_id=?", (s["source_file_id"],)).fetchone()
    outdated_source = bool(ver and ver["content_hash"] != s["source_version"])
    # Campos cujo valor oficial mudou depois que a sugestão foi criada: só então o revisor precisa confirmar.
    changed_since = []
    if s["kind"] == "update" and current and s["review_status"] == "pendente":
        for k, then in (s["current"] or {}).items():
            if current.get(k) != then:
                changed_since.append({"label": acts.FIELD_LABELS.get(k, k),
                                      "then": views.describe_value(k, then, names),
                                      "now": views.describe_value(k, current.get(k), names)})
    next_pending = _next_pending(conn, sid)
    # Exibição: pendente compara com o quadro de hoje; decidida, com o valor de antes da decisão
    shown = dict(s, current={k: current.get(k) for k in s["proposed"]}) if (current and s["review_status"] == "pendente") else s
    s["origin"] = views.sug_origin(s)
    return render(request, conn, "sugestao.html", s=s, rows=rows, current=current, names=names,
                  drows=views.sug_rows(shown, names), already=views.sug_already(s, names),
                  outdated_source=outdated_source, changed_since=changed_since, next_pending=next_pending,
                  n_left=len([x for x in sugg.list_suggestions(conn) if x["suggestion_id"] != sid]))


def _next_pending(conn, sid: int) -> int | None:
    """Próxima sugestão pendente na mesma ordem da lista, sem contar a atual."""
    for x in sugg.list_suggestions(conn):
        if x["suggestion_id"] != sid:
            return x["suggestion_id"]
    return None


@app.post("/sugestoes/{sid}/aceitar")
async def aceitar(request: Request, sid: int, conn=Depends(db)):
    me = require_member(request)
    form = await request.form()
    s = sugg.get(conn, sid)
    adjusted = {}
    if s and form.get("ajustar"):
        fields = list(s["proposed"]) + ([k for k in views.CREATE_FIELDS if k not in s["proposed"]]
                                        if s["kind"] == "create" else [])
        for k in fields:
            if k == "owners":
                adjusted[k] = form.getlist("owners")
            elif k in form:
                adjusted[k] = (form.get(k) or "").strip() or None
        # campo que a ata não trouxe e o revisor deixou em branco (ou no padrão "A fazer") não conta como ajuste
        adjusted = {k: v for k, v in adjusted.items()
                    if k in s["proposed"] or (v not in (None, "", []) and not (k == "status" and v == "A fazer"))}
        if "due_date" in adjusted and adjusted["due_date"] and not acts.valid_iso_date(adjusted["due_date"]):
            flash(request, "Prazo ajustado inválido. Use uma data válida.", "erro")
            return to(f"/sugestoes/{sid}")
    try:
        target = sugg.accept(conn, sid, me, adjusted=adjusted or None, note=(form.get("note") or "").strip() or None,
                             confirm_current_changed=bool(form.get("confirmar")))
    except sugg.ReviewError as e:
        flash(request, str(e), "erro")
        return to(f"/sugestoes/{sid}")
    t = conn.execute("SELECT title FROM activities WHERE activity_id=?", (target,)).fetchone()
    flash(request, f"Sugestão aceita. “{t['title'] if t else target}” foi atualizada no quadro de atividades.")
    return to(f"/sugestoes/{sid}#decisao")


@app.post("/sugestoes/{sid}/rejeitar")
def rejeitar(request: Request, sid: int, reason: str = Form(""), conn=Depends(db)):
    try:
        sugg.reject(conn, sid, require_member(request), reason)
    except sugg.ReviewError as e:
        flash(request, str(e), "erro")
        return to(f"/sugestoes/{sid}")
    flash(request, "Sugestão rejeitada. O quadro de atividades não mudou.")
    return to(f"/sugestoes/{sid}#decisao")


# ---------------------------------------------------------------------------
# E. O que mudou para mim
# ---------------------------------------------------------------------------
@app.get("/novidades", response_class=HTMLResponse)
def novidades(request: Request, desde: str = "visita", conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    opts = views.since_options()
    visit = conn.execute("SELECT prev_visit_at FROM member_visits WHERE member_id=?", (me,)).fetchone()
    if desde in opts:
        since = opts[desde]
    else:
        since = (visit["prev_visit_at"] if visit and visit["prev_visit_at"] else opts["7d"])
    data = views.changes_for_member(conn, me, since)
    # conflitos e dados que dependem de decisão humana (o resumo tem de dizer o que está em disputa)
    conflicts = [i for i in open_issues(conn) if i["kind"] in ("registro_homonimo", "registro_nao_definido",
                                                                 "registro_alterado", "responsavel_desconhecido")]
    return render(request, conn, "novidades.html", d=data, since=since, desde=desde, conflicts=conflicts,
                  has_visit=bool(visit and visit["prev_visit_at"]))


# ---------------------------------------------------------------------------
# F. Comece aqui
# ---------------------------------------------------------------------------
@app.get("/comece-aqui", response_class=HTMLResponse)
def comece(request: Request, pergunta: str = "", conn=Depends(db)):
    resposta = ask.ask(conn, make_qa_llm(), pergunta) if pergunta.strip() else None
    return render(request, conn, "comece.html", o=views.onboarding(conn, require_member(request)),
                  pergunta=pergunta.strip()[:ask.MAX_QUESTION], resposta=resposta)


# ---------------------------------------------------------------------------
# Fontes, pendências e sincronização
# ---------------------------------------------------------------------------
@app.get("/fontes", response_class=HTMLResponse)
def fontes(request: Request, q: str = "", tipo: str = "", situacao: str = "", conn=Depends(db)):
    all_rows = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY path, name")]
    rows = busca.filter_sources(conn, all_rows, q=q, tipo=tipo, situacao=situacao)
    filtro = {"q": q.strip(), "tipo": tipo, "situacao": situacao}
    official = get_setting(conn, "register_file_id")
    transcritos = {r["file_id"] for r in rows if r["sync_status"] == "ok" and transcribe.confirmed_info(conn, r["file_id"])}
    return render(request, conn, "fontes.html", rows=rows, official=official, transcritos=transcritos,
                  filtro=filtro, total=len(all_rows), filtering=bool(filtro["q"] or tipo or situacao),
                  DOC_TIPOS=busca.DOC_TIPOS, DOC_SITUACOES=busca.DOC_SITUACOES,
                  official_name=next((r["name"] for r in all_rows if r["file_id"] == official), official),
                  bound_reason=get_setting(conn, "register_bound_reason"))


@app.get("/fontes/{file_id}", response_class=HTMLResponse)
def fonte(request: Request, file_id: str, conn=Depends(db)):
    s = conn.execute("SELECT * FROM sources WHERE file_id=?", (file_id,)).fetchone()
    if not s:
        return render(request, conn, "erro.html", status_code=404, message="Documento não encontrado.")
    versions = [dict(r) for r in conn.execute(
        "SELECT id, drive_version, content_hash, modified_at, name, fetched_at, extracted_text, extracted_json "
        "FROM source_versions WHERE file_id=? ORDER BY id DESC", (file_id,))]
    diff = []
    if len(versions) >= 2 and versions[0]["extracted_text"] is not None:
        diff = views.diff_versions(versions[1]["extracted_text"], versions[0]["extracted_text"])
    current = versions[0] if versions else None
    table = json.loads(current["extracted_json"]) if current and current["extracted_json"] else None
    notes = views.left_out([dict(r) for r in conn.execute(
        "SELECT * FROM extraction_notes WHERE file_id=? AND source_version=? ORDER BY id", (file_id, s["content_hash"]))])
    sug = conn.execute("SELECT * FROM suggestions WHERE source_file_id=? ORDER BY suggestion_id DESC", (file_id,)).fetchall()
    names = views.member_names(conn)
    tr = transcribe.current(conn, file_id)
    if tr:
        tr["by"] = names.get(tr["created_by"], tr["created_by"])
        tr["confirmed_by_name"] = names.get(tr["confirmed_by"], tr["confirmed_by"])
        tr["edited"] = tr["text"].strip() != tr["ai_text"].strip()
    return render(request, conn, "fonte.html", s=dict(s), versions=versions, diff=diff, current=current, table=table,
                  notes=notes, sug=sug, meta=json.loads(s["doc_meta"] or "{}"), tr=tr,
                  can_transcribe=transcribe.can_transcribe(s), llm_on=settings.llm_enabled)


@app.get("/fontes/{file_id}/original")
def fonte_original(file_id: str, conn=Depends(db)):
    """Prévia do original (imagem ou 1ª página do PDF) para conferir a transcrição ao lado."""
    from fastapi.responses import Response
    s = conn.execute("SELECT * FROM sources WHERE file_id=?", (file_id,)).fetchone()
    if not s or not transcribe.mime_for(s):
        return Response(status_code=404)
    try:
        data = make_source(conn).fetch(transcribe.remote_file(s))
        body, mime = transcribe.preview_png(s, data)
    except Exception:
        log.exception("Prévia do original falhou")
        return Response(status_code=502)
    return Response(body, media_type=mime, headers={"Cache-Control": "private, max-age=300"})


@app.post("/fontes/{file_id}/transcrever")
def transcrever(request: Request, file_id: str, conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    try:
        transcribe.transcribe(conn, make_source(conn), make_vision_llm(), file_id, me)
        flash(request, "Transcrição pronta. Confira com o original antes de confirmar: ela ainda não vale.")
    except (transcribe.TranscriptionError, SourceError, drive_auth.DriveNotConnected) as e:
        flash(request, str(e), "erro")
    return to(f"/fontes/{file_id}#transcricao")


@app.post("/fontes/{file_id}/transcricao/confirmar")
def transcricao_confirmar(request: Request, file_id: str, text: str = Form(""), conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    try:
        transcribe.confirm(conn, file_id, me, text)
    except transcribe.TranscriptionError as e:
        flash(request, str(e), "erro")
        return to(f"/fontes/{file_id}#transcricao")
    r = do_sync("manual")   # a transcrição conferida entra agora pelo caminho de qualquer documento
    if r.get("status") in ("ok", "parcial"):
        flash(request, "Transcrição confirmada. O texto passou a valer e o documento foi analisado como os demais.")
    else:
        flash(request, "Transcrição confirmada. Ela entra na próxima atualização com o Drive.", "aviso")
    return to(f"/fontes/{file_id}")


@app.post("/fontes/{file_id}/transcricao/descartar")
def transcricao_descartar(request: Request, file_id: str, conn=Depends(db)):
    if not require_member(request):
        return to("/entrar")
    transcribe.discard(conn, file_id)
    flash(request, "Transcrição descartada. O arquivo continua como não processado.")
    return to(f"/fontes/{file_id}")


@app.post("/fontes/{file_id}/reanalisar")
def reanalisar(request: Request, file_id: str, conn=Depends(db)):
    """Pede nova análise da versão atual (ex.: após melhorar o prompt ou quando a IA falhou)."""
    me = require_member(request)
    if not me:
        return to("/entrar")
    conn.execute("UPDATE sources SET last_processed_hash=NULL, status_message=? WHERE file_id=? AND role='ata'",
                 (f"Nova análise solicitada por {me}; será feita na próxima sincronização.", file_id))
    from .db import set_setting
    set_setting(conn, f"ia_backoff:{file_id}", None)
    # Relê o cabeçalho do texto já guardado (o leitor de cabeçalho pode ter melhorado)
    from .extractors import parse_text_document
    from .db import dumps
    v = conn.execute("SELECT sv.extracted_text FROM sources s JOIN source_versions sv ON sv.file_id=s.file_id "
                     "AND sv.content_hash=s.content_hash WHERE s.file_id=?", (file_id,)).fetchone()
    if v and v["extracted_text"]:
        meta = parse_text_document(v["extracted_text"]).meta
        conn.execute("UPDATE sources SET doc_meta=?, doc_status=? WHERE file_id=?",
                     (dumps(meta), meta.get("status"), file_id))
    flash(request, "Nova análise pedida. Ela acontece na próxima atualização com o Drive (ou use “Atualizar agora”).")
    return to(f"/fontes/{file_id}")


@app.get("/pendencias", response_class=HTMLResponse)
def pendencias(request: Request, conn=Depends(db)):
    items = open_issues(conn)
    for i in items:
        i["source"] = views.source_link(conn, i["file_id"])
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM issues WHERE status='resolvida' ORDER BY resolved_at DESC LIMIT 20")]
    return render(request, conn, "pendencias.html", items=items, closed=closed)


@app.post("/pendencias/{issue_id}/resolver")
def resolver(request: Request, issue_id: int, resolution: str = Form(""), conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    if not resolution.strip():
        flash(request, "Escreva o que foi decidido para marcar a pendência como resolvida.", "erro")
        return to("/pendencias")
    resolve_issue(conn, issue_id=issue_id, by=me, resolution=resolution.strip())
    flash(request, "Pendência resolvida. A decisão ficou registrada.")
    return to("/pendencias")


@app.get("/sincronizacao", response_class=HTMLResponse)
def sincronizacao(request: Request, conn=Depends(db)):
    runs = views.group_runs(conn.execute("SELECT * FROM sync_runs ORDER BY run_id DESC LIMIT 40").fetchall())
    connected = bool(get_setting(conn, "google_token"))
    calls = conn.execute("SELECT COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o FROM llm_calls WHERE ok=1").fetchone()
    folder = json.loads(get_setting(conn, "drive_folder") or "null")
    return render(request, conn, "sincronizacao.html", runs=runs, connected=connected, settings=settings, calls=calls,
                  folder=folder)


@app.post("/sincronizar")
def sincronizar(request: Request):
    r = do_sync("manual")
    if r.get("status") in ("ok", "parcial"):
        changed = (f"{r['processed']} {'lido agora' if r['processed'] == 1 else 'lidos agora'}" if r["processed"]
                   else "nenhum mudou")
        if r.get("ignored"):
            changed += f", {r['ignored']} não {'processado' if r['ignored'] == 1 else 'processados'} (formato)"
        errors = f", {r['errors']} com erro" if r["errors"] else ""
        flash(request, f"Atualizado com o Drive: {r['files_seen']} arquivos conferidos, {changed}{errors}.",
              "ok" if r["status"] == "ok" else "aviso")
    elif r.get("status") == "ocupado":
        flash(request, "Já existe uma atualização em andamento. Aguarde cerca de 1 minuto e recarregue a página.",
              "aviso")
    else:
        flash(request, f"A atualização com o Drive falhou: {r.get('message')}", "erro")
    return to(request.headers.get("referer") or "/sincronizacao")


# ---------------------------------------------------------------------------
# OAuth Google Drive
# ---------------------------------------------------------------------------
@app.get("/auth/login")
def auth_login(request: Request):
    if not settings.drive_configured:
        flash(request, "Preencha GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET e DRIVE_TEST_FOLDER_ID no .env.", "erro")
        return to("/sincronizacao")
    url, state, verifier = drive_auth.authorization_url(settings)
    request.session["oauth_state"] = state
    request.session["oauth_verifier"] = verifier
    return RedirectResponse(url, status_code=302)


@app.get("/auth/callback")
def auth_callback(request: Request, conn=Depends(db)):
    state = request.query_params.get("state")
    if request.query_params.get("error"):
        flash(request, f"Autorização não concedida: {request.query_params.get('error')}", "erro")
        return to("/sincronizacao")
    if not state or state != request.session.pop("oauth_state", None):
        flash(request, "A conexão com o Google expirou ou foi interrompida. Tente conectar de novo.", "erro")
        return to("/sincronizacao")
    try:
        drive_auth.finish_authorization(conn, settings, str(request.url), state, request.session.pop("oauth_verifier", None))
    except Exception as e:
        log.warning("Falha no OAuth: %s", type(e).__name__)  # não registra código nem token
        flash(request, "Não foi possível concluir a autorização do Google. Confira o redirect URI no Console.", "erro")
        return to("/sincronizacao")
    flash(request, "Google Drive conectado. Fazendo a primeira atualização…")
    threading.Thread(target=do_sync, args=("manual",), daemon=True).start()
    return to("/sincronizacao")


@app.post("/auth/desconectar")
def auth_desconectar(request: Request, conn=Depends(db)):
    drive_auth.disconnect(conn)
    flash(request, "Drive desconectado e autorização revogada.")
    return to("/sincronizacao")
