"""Montagem dos dados das telas: resumo pessoal, "Comece aqui" e formatação.

Os campos factuais vêm sempre dos registros (atividades, eventos, sugestões,
fontes), cada um com link verificável. Nada aqui é inventado por IA.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

from . import activities as acts
from .config import TZ
from .extractors import normalize

MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
MONTHS_FULL = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro",
               "outubro", "novembro", "dezembro"]


def today() -> date:
    return datetime.now(TZ).date()


def fmt_date(iso: str | None) -> str:
    if not iso:
        return "a definir"
    d = date.fromisoformat(iso[:10])
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def fmt_ts(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone(TZ)
    return f"{dt.day:02d}/{dt.month:02d} {dt.hour:02d}:{dt.minute:02d}"


def fmt_when(iso: str | None) -> str:
    """Só a hora quando é hoje; data e hora nos outros dias."""
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso).astimezone(TZ)
    return f"{dt.hour:02d}:{dt.minute:02d}" if dt.date() == today() else fmt_ts(iso)


def due_info(iso: str | None, status: str | None = None) -> dict:
    """Situação do prazo em São Paulo. Datas passadas são sinalizadas, nunca alteradas."""
    if not iso:
        return {"label": "Prazo a definir", "kind": "none"}
    if status == "Concluída":
        return {"label": f"Prazo {fmt_date(iso)}", "kind": "done"}
    days = (date.fromisoformat(iso) - today()).days
    if days < 0:
        return {"label": f"Vencida há {-days} dia{'s' if -days > 1 else ''}", "kind": "overdue"}
    if days == 0:
        return {"label": "Vence hoje", "kind": "soon"}
    if days <= 3:
        return {"label": f"Vence em {days} dia{'s' if days > 1 else ''}", "kind": "soon"}
    return {"label": f"Vence em {days} dias", "kind": "ok"}


def date_parts(iso: str | None, status: str | None = None) -> dict | None:
    """Partes para a coluna de data: {'d': '5', 'm': 'outubro', 'kind': ...}."""
    if not iso:
        return None
    d = date.fromisoformat(iso[:10])
    kind = due_info(iso, status)["kind"]
    return {"d": str(d.day), "m": MONTHS_FULL[d.month - 1], "kind": kind if kind in ("overdue", "soon") else ""}


def member_names(conn) -> dict[str, str]:
    return {r["member_id"]: r["display_name"] for r in conn.execute("SELECT * FROM members")}


def describe_value(field: str, value, names: dict) -> str:
    if value is None or value == [] or value == "":
        return "a definir" if field == "due_date" else "—"
    if field == "owners":
        return ", ".join(names.get(v, v) for v in value)
    if field == "due_date":
        return fmt_date(value)
    return str(value)


def source_link(conn, file_id: str | None) -> dict | None:
    if not file_id:
        return None
    r = conn.execute("SELECT * FROM sources WHERE file_id=?", (file_id,)).fetchone()
    if not r:
        return {"name": file_id, "url": None, "status": "desconhecida"}
    return {"file_id": file_id, "name": r["name"], "url": r["web_url"], "status": r["sync_status"], "role": r["role"]}


# ---------------------------------------------------------------------------
# E. O que mudou para mim
# ---------------------------------------------------------------------------
def changes_for_member(conn: sqlite3.Connection, member_id: str, since_iso: str) -> dict:
    names = member_names(conn)
    mine_now = {r["activity_id"] for r in conn.execute("SELECT activity_id FROM activity_owners WHERE member_id=?",
                                                        (member_id,))}
    confirmed = []
    for e in conn.execute("SELECT * FROM activity_events WHERE ts > ? ORDER BY ts DESC", (since_iso,)):
        before = json.loads(e["before_json"]) if e["before_json"] else None
        after = json.loads(e["after_json"]) if e["after_json"] else {}
        if before is None and e["actor_id"] == "sistema":
            continue  # importação inicial da planilha: é o ponto de partida, não uma "novidade"
        if before == {} and not after:
            continue  # "continua valendo": confirmação, não mudança
        owners_involved = set(after.get("owners") or []) | set((before or {}).get("owners") or [])
        if e["activity_id"] not in mine_now and member_id not in owners_involved:
            continue
        a = acts.snapshot(conn, e["activity_id"]) or {}
        if before is None:
            changes = [("Criada", "", a.get("title", ""))]
        else:
            changes = [(acts.FIELD_LABELS.get(k, k), describe_value(k, before.get(k), names),
                        describe_value(k, after.get(k), names)) for k in after]
        confirmed.append({"activity_id": e["activity_id"], "title": a.get("title"), "ts": e["ts"],
                          "actor": names.get(e["actor_id"], e["actor_id"]), "reason": e["reason"],
                          "changes": changes, "source": source_link(conn, e["source_file_id"])})

    def affects(s):
        if s["target_activity_id"] and s["target_activity_id"] in mine_now:
            return True
        return member_id in (json.loads(s["proposed_fields"]).get("owners") or [])

    suggestions = []
    for s in conn.execute("SELECT * FROM suggestions WHERE (review_status='pendente' OR reviewed_at > ?) "
                          "ORDER BY suggestion_id DESC", (since_iso,)):
        if affects(s):
            t = conn.execute("SELECT title FROM activities WHERE activity_id=?", (s["target_activity_id"],)).fetchone()
            suggestions.append({"id": s["suggestion_id"], "kind": s["kind"], "target": s["target_activity_id"],
                                "target_title": t["title"] if t else None,
                                "status": s["review_status"], "proposed": json.loads(s["proposed_fields"]),
                                "source": source_link(conn, s["source_file_id"]), "evidence": s["evidence"]})

    attention = []
    for a in acts.list_activities(conn, member_id):
        info = due_info(a["due_date"], a["status"])
        if a["status"] == "Bloqueada" or info["kind"] in ("overdue", "soon"):
            attention.append({**a, "due": info})

    new_sources = [dict(r) for r in conn.execute(
        "SELECT file_id, name, web_url, role, first_seen_at, sync_status FROM sources WHERE first_seen_at > ? ORDER BY first_seen_at DESC",
        (since_iso,))]
    new_ids = {r["file_id"] for r in conn.execute("SELECT file_id FROM sources WHERE first_seen_at > ?", (since_iso,))}
    edited_sources = [dict(r) for r in conn.execute(
        "SELECT s.file_id, s.name, s.web_url, s.role, MAX(v.fetched_at) AS edited_at FROM sources s "
        "JOIN source_versions v ON v.file_id = s.file_id WHERE v.fetched_at > ? "
        "AND (SELECT COUNT(*) FROM source_versions v2 WHERE v2.file_id = s.file_id) > 1 "
        "GROUP BY s.file_id ORDER BY edited_at DESC", (since_iso,)) if r["file_id"] not in new_ids]
    unavailable = [dict(r) for r in conn.execute(
        "SELECT file_id, name, status_message FROM sources WHERE sync_status IN ('indisponivel','erro')")]
    nothing = not (confirmed or suggestions or new_sources or edited_sources)
    return {"confirmed": confirmed, "suggestions": suggestions, "attention": attention, "new_sources": new_sources,
            "edited_sources": edited_sources, "unavailable": unavailable, "nothing_changed": nothing}


# ---------------------------------------------------------------------------
# F. Comece aqui
# ---------------------------------------------------------------------------
def _doc(conn, role: str):
    r = conn.execute(
        "SELECT s.*, sv.extracted_text FROM sources s LEFT JOIN source_versions sv "
        "ON sv.file_id=s.file_id AND sv.content_hash=s.content_hash "
        "WHERE s.role=? AND s.sync_status IN ('ok','indisponivel') ORDER BY s.first_seen_at LIMIT 1", (role,)).fetchone()
    return dict(r) if r else None


def _body_paragraphs(text: str) -> list[str]:
    """Parágrafos depois do cabeçalho (título + linhas chave: valor)."""
    out, started = [], False
    for block in re.split(r"\n\s*\n", text or ""):
        b = block.strip()
        if not b or b.startswith("#") and not started:
            continue
        if re.match(r"^[a-z_]+\s*:", b) and not started:
            continue
        started = True
        out.append(b)
    return out


def section(text: str, heading: str) -> list[str]:
    lines, inside = [], False
    for line in (text or "").splitlines():
        if line.startswith("#"):
            inside = normalize(line.lstrip("#").strip()) == normalize(heading)
            continue
        if inside and line.strip():
            lines.append(re.sub(r"^\d+\.\s*", "", line.strip().lstrip("-").strip()).replace("`", ""))
    return lines


GAP_PATTERNS = ["a confirmar", "por confirmar", "sera aprovad", "provisori", "nao e um texto oficial",
                "ainda nao", "incomplet"]


def onboarding(conn: sqlite3.Connection, member_id: str | None) -> dict:
    estado = _doc(conn, "estado_atual")
    guia = _doc(conn, "guia")
    indice = _doc(conn, "indice")
    purpose, gaps = [], []
    if estado:
        meta = json.loads(estado["doc_meta"] or "{}")
        paras = _body_paragraphs(estado["extracted_text"])
        purpose = paras[:2]
        if normalize(meta.get("status", "")) != "ativo":
            gaps.append(f"O ESTADO-ATUAL.md está marcado como '{meta.get('status', 'sem status')}': as informações podem estar incompletas.")
        if meta.get("responsavel_por_confirmar"):
            gaps.append(f"Responsável por confirmar este resumo: {meta['responsavel_por_confirmar']}.")
        for p in paras:
            for sentence in re.split(r"(?<=[.!?])\s+", p):
                n = normalize(sentence)
                # "Em caso de dúvida… mostrar 'responsável a confirmar'" é orientação ao app, não uma lacuna
                if any(g in n for g in GAP_PATTERNS) and not any(x in n for x in ("mostrar", "nao completar")):
                    gaps.append(sentence)
    else:
        gaps.append("Não há ESTADO-ATUAL.md na pasta: propósito e frentes não confirmados.")
    # Só linhas no formato "Frente: pessoas..." (descarta notas soltas da seção)
    fronts = [l for l in (section(guia["extracted_text"], "Frentes e pessoas") if guia else [])
              if re.match(r"^[^:]{2,30}:\s", l)]
    if not fronts and estado:
        fronts = [p for p in _body_paragraphs(estado["extracted_text"]) if "frente" in normalize(p)][:1]
    steps = section(guia["extracted_text"], "Para um membro novo") if guia else []

    references = [dict(r) for r in conn.execute(
        "SELECT file_id, name, web_url, role, sync_status, doc_status FROM sources "
        "WHERE role IN ('indice','estado_atual','guia','registro_oficial') ORDER BY role")]
    historic = [dict(r) for r in conn.execute(
        "SELECT name, web_url, doc_meta FROM sources WHERE role='historico'")]
    for h in historic:
        h["meta"] = json.loads(h["doc_meta"] or "{}")

    first_action = None
    if member_id:
        mine = acts.list_activities(conn, member_id)
        workable = [a for a in mine if a["status"] != "Bloqueada"] or mine
        if workable:
            first_action = workable[0]
    return {"estado": estado, "guia": guia, "indice": indice, "purpose": purpose, "gaps": gaps, "fronts": fronts,
            "steps": steps, "references": references, "historic": historic, "first_action": first_action}


def diff_versions(old: str, new: str) -> list[tuple[str, str]]:
    """Diferença linha a linha entre duas versões de texto: [('+'|'-'|' ', linha)]."""
    import difflib
    out = []
    for line in difflib.ndiff((old or "").splitlines(), (new or "").splitlines()):
        tag = line[:1]
        if tag in ("+", "-"):
            out.append((tag, line[2:]))
    return out


def since_options() -> dict[str, str]:
    now = datetime.now(TZ)
    return {"1d": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            "7d": (now - timedelta(days=7)).isoformat(timespec="seconds"),
            "30d": (now - timedelta(days=30)).isoformat(timespec="seconds")}


# ---------------------------------------------------------------------------
# Textos para a tela: plural correto e vocabulário de quem usa (não o do código)
# ---------------------------------------------------------------------------
def plural(n: int, singular: str, plural_form: str) -> str:
    return f"{n} {singular if n == 1 else plural_form}"


def analysis_summary(created: int, unchanged: int, hypotheses: int, rejected: int) -> str:
    """Resultado da leitura de uma ata em uma frase curta."""
    parts = []
    if created:
        parts.append(plural(created, "sugestão criada", "sugestões criadas"))
    if unchanged:
        parts.append(plural(unchanged, "item já estava no quadro", "itens já estavam no quadro"))
    if hypotheses:
        parts.append(plural(hypotheses, "ideia sem decisão", "ideias sem decisão"))
    if rejected:
        parts.append(plural(rejected, "descartada na checagem", "descartadas na checagem"))
    if not parts:
        return "Nada novo nesta ata"
    return (", ".join(parts[:-1]) + " e " + parts[-1]) if len(parts) > 1 else parts[0]


_ANALYSIS_RE = re.compile(r"(\d+) sugestão\(ões\), (\d+) já no registro, (\d+) ideia\(s\) sem decisão, "
                          r"(\d+) proposta\(s\) barrada\(s\) pela validação")
_PLURAL_RE = re.compile(r"\b(\d+)((?:\s+[^\s(),.;:]+\((?:s|ões|es)\))+)")


def _fix_plurals(text: str) -> str:
    def repl(m):
        n = int(m.group(1))
        def word(w):
            base, suf = w.group(1), w.group(2)
            if n == 1:
                return base
            return base[:-2] + "ões" if suf == "ões" and base.endswith("ão") else base + suf
        return m.group(1) + re.sub(r"([^\s(]+)\((s|ões|es)\)", word, m.group(2))
    return _PLURAL_RE.sub(repl, text)


def humano(text) -> str:
    """Reescreve mensagens gravadas pelo sincronizador (inclusive as antigas, já salvas no banco)
    no vocabulário da tela: 'quadro de atividades', plurais escritos, sem jargão."""
    if not text:
        return text or ""
    t = str(text)
    m = _ANALYSIS_RE.fullmatch(t.strip())
    if m:
        return analysis_summary(*map(int, m.groups()))
    t = {"indexado": "Lido como referência",
         "pendência de conflito registrada": "Conflito registrado em Pendências",
         "Indexada. IA desativada: sem sugestões por enquanto.": "Lida, mas a IA está desligada: sem sugestões por enquanto."
         }.get(t.strip(), t)
    t = t.replace("PDF sem texto selecionável (provavelmente digitalizado). OCR está fora do escopo.",
                  "PDF escaneado, sem texto para ler. O protótipo não lê texto dentro de imagens.")
    t = re.sub(r"Formato ainda não processado \(([^)]*)\)\.", r"O protótipo ainda não lê este formato (\1).", t)
    t = t.replace("Arquivo .docx não é lido diretamente. Converta para Google Docs nativo no Drive.",
                  "Arquivo .docx não é lido. No Drive, use Arquivo > Salvar como Documentos Google.")
    t = re.sub(r"Importação inicial do registro oficial \(([^,]+), linha (\d+)\)",
               r"Importada da planilha (aba \1, linha \2)", t)
    t = t.replace("Sincronizar agora", "Atualizar agora").replace("sincronização", "atualização")
    t = t.replace("registro oficial", "quadro de atividades").replace("Registro oficial", "Quadro de atividades")
    t = t.replace("já no registro", "já no quadro").replace("Fonte indisponível", "Documento indisponível")
    return _fix_plurals(t)


ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
DOC_STATUS = {"ativo": "em vigor", "parcial": "incompleto", "deprecated": "obsoleto", "obsoleto": "obsoleto",
              "substituido": "substituído", "rascunho": "rascunho", "arquivado": "arquivado", "historico": "histórico"}


def doc_status(value) -> str:
    return DOC_STATUS.get(str(value).strip().lower(), str(value)) if value else ""


def doc_meta_items(meta: dict | None) -> list[str]:
    """Cabeçalho 'chave: valor' de um documento em frases curtas."""
    out = []
    for k, v in (meta or {}).items():
        v = str(v)
        day = fmt_date(v) if ISO_DAY.fullmatch(v) else v
        label = {"data_da_reuniao": f"Reunião de {day}", "status": doc_status(v),
                 "atualizado_em": f"atualizado em {day}", "criado_em": f"criado em {day}",
                 "responsavel_por_confirmar": f"quem confirma: {v}", "substitui": f"substitui {v}",
                 "substituido_por": "substituído por " + re.sub(r"\d{4}-\d{2}-\d{2}", lambda m: fmt_date(m.group()), v), "escopo": v}.get(k, f"{k.replace('_', ' ')}: {v}")
        out.append(label)
    return out


def sheet_ref(value: str | None) -> str:
    """'Atividades!linha 3' -> 'aba Atividades, linha 3'."""
    if not value:
        return ""
    m = re.fullmatch(r"(.+)!linha (\d+)", value.strip())
    return f"aba {m.group(1)}, linha {m.group(2)}" if m else value


def group_runs(runs: list) -> list[dict]:
    """Junta verificações seguidas sem mudança numa linha só ('12 verificações sem mudança')."""
    out: list[dict] = []
    for r in runs:
        r = dict(r)
        quiet = r["status"] == "ok" and not r["processed"] and not r["errors"]
        if quiet and out and out[-1].get("quiet"):
            g = out[-1]
            g["count"] += 1
            g["first"] = r["started_at"]
            continue
        r["quiet"] = quiet
        r["count"] = 1
        r["first"] = r["started_at"]
        out.append(r)
    return out


def humano_linhas(text) -> str:
    """Aplica `humano` a cada linha 'arquivo: mensagem' do resumo de uma verificação."""
    out = []
    for line in str(text or "").splitlines():
        name, sep, rest = line.partition(": ")
        out.append(f"{name}: {humano(rest)}" if sep else humano(line))
    return "\n".join(out)
