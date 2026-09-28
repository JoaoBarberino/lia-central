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
from . import drive_auth
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
templates.env.globals.update(
    NAMES=_NAMES, date_parts=views.date_parts,
    fmt_date=views.fmt_date, fmt_ts=views.fmt_ts, fmt_when=views.fmt_when, due_info=views.due_info, ROLE_LABELS=ROLE_LABELS,
    FIELD_LABELS=acts.FIELD_LABELS, ISSUE_LABELS=ISSUE_LABELS, STATUSES=acts.STATUSES,
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


def render(request: Request, conn, template: str, **ctx):
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
    return templates.TemplateResponse(request, template, ctx)


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
        flash(request, "Pessoa não encontrada.", "erro")
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
def home(request: Request, ordem: str = "prazo", conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    items = acts.list_activities(conn, me)
    if ordem == "estado":
        items.sort(key=lambda a: (a["status"] != "Bloqueada", a["due_date"] or "9999"))
    mine_pending = [s for s in sugg.list_suggestions(conn) if (s["target_activity_id"] in {a["activity_id"] for a in items})
                    or me in (s["proposed"].get("owners") or [])]
    stats = {
        "open": len(items),
        "soon": sum(1 for a in items if views.due_info(a["due_date"], a["status"])["kind"] in ("soon", "overdue")),
        "blocked": sum(1 for a in items if a["status"] == "Bloqueada"),
        "pending_me": len(mine_pending),
        "to_review": conn.execute("SELECT COUNT(*) FROM suggestions WHERE review_status='pendente'").fetchone()[0],
    }
    return render(request, conn, "minhas.html", items=items, ordem=ordem, mine_pending=mine_pending, stats=stats)


@app.get("/atividades", response_class=HTMLResponse)
def todas(request: Request, concluidas: int = 0, conn=Depends(db)):
    return render(request, conn, "atividades.html", items=acts.list_activities(conn, include_done=bool(concluidas)),
                  concluidas=concluidas)


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
    flash(request, f"Atividade {new_id} criada.")
    return to(f"/atividades/{new_id}")


@app.get("/atividades/{activity_id}", response_class=HTMLResponse)
def detalhe(request: Request, activity_id: str, conn=Depends(db)):
    a = acts.snapshot(conn, activity_id)
    if not a:
        return render(request, conn, "erro.html", message=f"Atividade {activity_id} não encontrada.")
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
    return render(request, conn, "atividade.html", a=row, history=history, refs=refs, pend=pend, names=names)


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
    flash(request, f"{len(diff)} campo(s) alterado(s)." if diff else "Nada mudou.")
    return to(f"/atividades/{activity_id}")


@app.post("/atividades/{activity_id}/estado")
def mudar_estado(request: Request, activity_id: str, status: str = Form(...), reason: str = Form(""), conn=Depends(db)):
    me = require_member(request)
    if not me:
        return to("/entrar")
    if status not in acts.STATUSES:
        flash(request, "Estado inválido.", "erro")
        return to(f"/atividades/{activity_id}")
    changes = {"status": status}
    if status == "Bloqueada" and reason.strip():
        changes["notes"] = reason.strip()
    acts.update_activity(conn, activity_id, changes, actor_id=me, reason=reason.strip() or f"Marcada como {status}")
    flash(request, f"{activity_id}: {status}.")
    return to(f"/atividades/{activity_id}")


# ---------------------------------------------------------------------------
# D. Revisar sugestões
# ---------------------------------------------------------------------------
@app.get("/sugestoes", response_class=HTMLResponse)
def sugestoes(request: Request, estado: str = "pendente", conn=Depends(db)):
    items = sugg.list_suggestions(conn, None if estado == "todas" else estado)
    names = views.member_names(conn)
    for s in items:
        s["source"] = views.source_link(conn, s["source_file_id"])
    notes = [dict(r) | {"source": views.source_link(conn, r["file_id"])} for r in conn.execute(
        "SELECT n.* FROM extraction_notes n JOIN sources s ON s.file_id = n.file_id AND s.content_hash = n.source_version "
        "WHERE n.kind IN ('hipotese','barrada_validacao') ORDER BY n.id DESC")]
    return render(request, conn, "sugestoes.html", items=items, estado=estado, names=names, notes=notes)


@app.get("/sugestoes/{sid}", response_class=HTMLResponse)
def sugestao(request: Request, sid: int, conn=Depends(db)):
    s = sugg.get(conn, sid)
    if not s:
        return render(request, conn, "erro.html", message="Sugestão não encontrada.")
    names = views.member_names(conn)
    s["source"] = views.source_link(conn, s["source_file_id"])
    current = acts.snapshot(conn, s["target_activity_id"]) if s["target_activity_id"] else None
    rows = []
    for k, v in s["proposed"].items():
        rows.append({"field": k, "label": acts.FIELD_LABELS.get(k, k),
                     "official": views.describe_value(k, current.get(k), names) if current else "—",
                     "proposed": views.describe_value(k, v, names), "raw": v})
    ver = conn.execute("SELECT content_hash FROM sources WHERE file_id=?", (s["source_file_id"],)).fetchone()
    outdated_source = bool(ver and ver["content_hash"] != s["source_version"])
    return render(request, conn, "sugestao.html", s=s, rows=rows, current=current, names=names,
                  outdated_source=outdated_source)


@app.post("/sugestoes/{sid}/aceitar")
async def aceitar(request: Request, sid: int, conn=Depends(db)):
    me = require_member(request)
    form = await request.form()
    s = sugg.get(conn, sid)
    adjusted = {}
    if s and form.get("ajustar"):
        for k in s["proposed"]:
            if k == "owners":
                adjusted[k] = form.getlist("owners")
            elif k in form:
                adjusted[k] = (form.get(k) or "").strip() or None
        if "due_date" in adjusted and adjusted["due_date"] and not acts.valid_iso_date(adjusted["due_date"]):
            flash(request, "Prazo ajustado inválido. Use uma data válida.", "erro")
            return to(f"/sugestoes/{sid}")
    try:
        target = sugg.accept(conn, sid, me, adjusted=adjusted or None, note=(form.get("note") or "").strip() or None,
                             confirm_current_changed=bool(form.get("confirmar")))
    except sugg.ReviewError as e:
        flash(request, str(e), "erro")
        return to(f"/sugestoes/{sid}")
    flash(request, f"Sugestão aceita. {target} atualizada no registro oficial.")
    return to(f"/atividades/{target}")


@app.post("/sugestoes/{sid}/rejeitar")
def rejeitar(request: Request, sid: int, reason: str = Form(""), conn=Depends(db)):
    try:
        sugg.reject(conn, sid, require_member(request), reason)
    except sugg.ReviewError as e:
        flash(request, str(e), "erro")
        return to(f"/sugestoes/{sid}")
    flash(request, "Sugestão rejeitada. O registro oficial não mudou.")
    return to("/sugestoes")


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
    return render(request, conn, "novidades.html", d=data, since=since, desde=desde,
                  has_visit=bool(visit and visit["prev_visit_at"]))


# ---------------------------------------------------------------------------
# F. Comece aqui
# ---------------------------------------------------------------------------
@app.get("/comece-aqui", response_class=HTMLResponse)
def comece(request: Request, conn=Depends(db)):
    return render(request, conn, "comece.html", o=views.onboarding(conn, require_member(request)))


# ---------------------------------------------------------------------------
# Fontes, pendências e sincronização
# ---------------------------------------------------------------------------
@app.get("/fontes", response_class=HTMLResponse)
def fontes(request: Request, conn=Depends(db)):
    rows = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY path, name")]
    official = get_setting(conn, "register_file_id")
    return render(request, conn, "fontes.html", rows=rows, official=official,
                  bound_reason=get_setting(conn, "register_bound_reason"))


@app.get("/fontes/{file_id}", response_class=HTMLResponse)
def fonte(request: Request, file_id: str, conn=Depends(db)):
    s = conn.execute("SELECT * FROM sources WHERE file_id=?", (file_id,)).fetchone()
    if not s:
        return render(request, conn, "erro.html", message="Fonte não encontrada.")
    versions = [dict(r) for r in conn.execute(
        "SELECT id, drive_version, content_hash, modified_at, name, fetched_at, extracted_text, extracted_json "
        "FROM source_versions WHERE file_id=? ORDER BY id DESC", (file_id,))]
    diff = []
    if len(versions) >= 2 and versions[0]["extracted_text"] is not None:
        diff = views.diff_versions(versions[1]["extracted_text"], versions[0]["extracted_text"])
    current = versions[0] if versions else None
    table = json.loads(current["extracted_json"]) if current and current["extracted_json"] else None
    notes = conn.execute("SELECT * FROM extraction_notes WHERE file_id=? AND source_version=? ORDER BY id",
                         (file_id, s["content_hash"])).fetchall()
    sug = conn.execute("SELECT * FROM suggestions WHERE source_file_id=? ORDER BY suggestion_id DESC", (file_id,)).fetchall()
    return render(request, conn, "fonte.html", s=dict(s), versions=versions, diff=diff, current=current, table=table,
                  notes=notes, sug=sug, meta=json.loads(s["doc_meta"] or "{}"))


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
    flash(request, "Nova análise solicitada. Ela acontece na próxima sincronização (automática ou 'Sincronizar agora').")
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
        flash(request, "Descreva a decisão tomada para encerrar a pendência.", "erro")
        return to("/pendencias")
    resolve_issue(conn, issue_id=issue_id, by=me, resolution=resolution.strip())
    flash(request, "Pendência encerrada. A decisão ficou registrada.")
    return to("/pendencias")


@app.get("/sincronizacao", response_class=HTMLResponse)
def sincronizacao(request: Request, conn=Depends(db)):
    runs = conn.execute("SELECT * FROM sync_runs ORDER BY run_id DESC LIMIT 20").fetchall()
    connected = bool(get_setting(conn, "google_token"))
    calls = conn.execute("SELECT COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o FROM llm_calls WHERE ok=1").fetchone()
    return render(request, conn, "sincronizacao.html", runs=runs, connected=connected, settings=settings, calls=calls)


@app.post("/sincronizar")
def sincronizar(request: Request):
    r = do_sync("manual")
    if r.get("status") in ("ok", "parcial"):
        flash(request, f"Sincronização concluída: {r['files_seen']} arquivo(s) vistos, {r['processed']} processado(s), "
                       f"{r['ignored']} ignorado(s), {r['errors']} com erro.", "ok" if r["status"] == "ok" else "aviso")
    elif r.get("status") == "ocupado":
        flash(request, "Uma sincronização já está em andamento (a automática ou outra manual). "
                       "Aguarde cerca de 1 minuto e recarregue a página para ver o resultado.", "aviso")
    else:
        flash(request, f"Sincronização não concluída: {r.get('message')}", "erro")
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
        flash(request, "Falha de verificação (state inválido). Tente conectar de novo.", "erro")
        return to("/sincronizacao")
    try:
        drive_auth.finish_authorization(conn, settings, str(request.url), state, request.session.pop("oauth_verifier", None))
    except Exception as e:
        log.warning("Falha no OAuth: %s", type(e).__name__)  # não registra código nem token
        flash(request, "Não foi possível concluir a autorização do Google. Confira o redirect URI no Console.", "erro")
        return to("/sincronizacao")
    flash(request, "Google Drive conectado. Rodando a primeira sincronização…")
    threading.Thread(target=do_sync, args=("manual",), daemon=True).start()
    return to("/sincronizacao")


@app.post("/auth/desconectar")
def auth_desconectar(request: Request, conn=Depends(db)):
    drive_auth.disconnect(conn)
    flash(request, "Drive desconectado e autorização revogada.")
    return to("/sincronizacao")
