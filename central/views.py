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
            suggestions.append({"id": s["suggestion_id"], "kind": s["kind"], "target": s["target_activity_id"],
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
        "SELECT name, web_url, role, sync_status, doc_status FROM sources "
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
