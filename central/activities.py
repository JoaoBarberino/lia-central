"""Registro oficial de atividades: leitura, criação e alteração com histórico.

Toda escrita passa por `create_activity` ou `update_activity`, que gravam um
evento (antes/depois, quem, por quê, fonte). Não existe UPDATE "solto".
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from .db import dumps, now_iso
from .extractors import normalize

STATUSES = ["A fazer", "Em andamento", "Bloqueada", "Concluída"]
SOON_DAYS = 3   # "perto do prazo": vence hoje ou nos próximos 3 dias (o mesmo critério em todas as telas)
_STATUS_BY_NORM = {normalize(s): s for s in STATUSES}
_STATUS_BY_NORM.update({"concluido": "Concluída", "feito": "Concluída", "bloqueado": "Bloqueada",
                        "fazer": "A fazer", "pendente": "A fazer"})

EDITABLE_FIELDS = ["title", "description", "next_step", "front", "priority", "status", "due_date", "notes", "owners"]
FIELD_LABELS = {
    "title": "Título", "description": "Descrição", "next_step": "Próximo passo", "front": "Frente",
    "priority": "Prioridade", "status": "Situação", "due_date": "Prazo", "notes": "Notas e bloqueios",
    "owners": "Responsáveis",
}
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_status(value: str | None) -> str:
    if not value:
        return "A fazer"
    return _STATUS_BY_NORM.get(normalize(value), value.strip())


def valid_iso_date(value: str | None) -> bool:
    if value is None:
        return True
    if not ISO_DATE.match(value):
        return False
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False


def members_by_name(conn: sqlite3.Connection) -> dict[str, str]:
    return {normalize(r["display_name"]): r["member_id"] for r in conn.execute("SELECT * FROM members")}


def parse_owner_names(conn: sqlite3.Connection, raw: str | None) -> tuple[list[str], list[str]]:
    """'Ana; Davi' -> (['U-A','U-D'], []). Nomes desconhecidos voltam na 2ª lista."""
    if not raw:
        return [], []
    by_name = members_by_name(conn)
    known, unknown = [], []
    for part in re.split(r"[;,/]|\s+e\s+", raw):
        name = part.strip()
        if not name:
            continue
        mid = by_name.get(normalize(name)) or (name if conn.execute(
            "SELECT 1 FROM members WHERE member_id = ?", (name,)).fetchone() else None)
        if mid:
            if mid not in known:
                known.append(mid)
        else:
            unknown.append(name)
    return known, unknown


def get_owners(conn: sqlite3.Connection, activity_id: str) -> list[str]:
    return [r["member_id"] for r in conn.execute(
        "SELECT member_id FROM activity_owners WHERE activity_id = ? ORDER BY member_id", (activity_id,))]


def snapshot(conn: sqlite3.Connection, activity_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
    if not row:
        return None
    snap = {f: row[f] for f in EDITABLE_FIELDS if f != "owners"}
    snap["owners"] = get_owners(conn, activity_id)
    return snap


def next_activity_id(conn: sqlite3.Connection) -> str:
    nums = [int(r["activity_id"].split("-")[1]) for r in conn.execute("SELECT activity_id FROM activities")
            if re.match(r"^ACT-\d+$", r["activity_id"])]
    return f"ACT-{(max(nums) + 1) if nums else 101}"


def _log_event(conn, activity_id, actor_id, before, after, reason, source_file_id=None, source_version=None,
               suggestion_id=None):
    conn.execute(
        """INSERT INTO activity_events (activity_id, actor_id, ts, before_json, after_json, reason,
                                        source_file_id, source_version, suggestion_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (activity_id, actor_id, now_iso(), dumps(before) if before is not None else None, dumps(after), reason,
         source_file_id, source_version, suggestion_id),
    )


def create_activity(conn: sqlite3.Connection, fields: dict, *, actor_id: str, creation_kind: str, reason: str,
                    activity_id: str | None = None, origin_label: str | None = None,
                    source_file_id: str | None = None, source_version: str | None = None,
                    suggestion_id: int | None = None) -> str:
    activity_id = activity_id or next_activity_id(conn)
    if not fields.get("title"):
        raise ValueError("Título é obrigatório.")
    if not valid_iso_date(fields.get("due_date")):
        raise ValueError("Prazo precisa estar no formato AAAA-MM-DD.")
    ts = now_iso()
    conn.execute(
        """INSERT INTO activities (activity_id, title, description, next_step, front, priority, status, due_date,
                                   notes, origin_label, creation_kind, created_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (activity_id, fields["title"], fields.get("description"), fields.get("next_step"), fields.get("front"),
         fields.get("priority"), normalize_status(fields.get("status")), fields.get("due_date"),
         fields.get("notes"), origin_label, creation_kind, actor_id, ts, ts),
    )
    for mid in fields.get("owners") or []:
        conn.execute("INSERT OR IGNORE INTO activity_owners (activity_id, member_id) VALUES (?,?)", (activity_id, mid))
    _log_event(conn, activity_id, actor_id, None, snapshot(conn, activity_id), reason, source_file_id,
               source_version, suggestion_id)
    return activity_id


def update_activity(conn: sqlite3.Connection, activity_id: str, changes: dict, *, actor_id: str, reason: str,
                    source_file_id: str | None = None, source_version: str | None = None,
                    suggestion_id: int | None = None) -> dict:
    """Aplica só os campos que realmente mudaram. Retorna {campo: (antes, depois)}."""
    before = snapshot(conn, activity_id)
    if before is None:
        raise ValueError(f"Atividade {activity_id} não existe.")
    if "status" in changes:
        changes["status"] = normalize_status(changes["status"])
    if "due_date" in changes and not valid_iso_date(changes["due_date"]):
        raise ValueError("Prazo precisa estar no formato AAAA-MM-DD.")
    diff = {}
    for field, value in changes.items():
        if field not in EDITABLE_FIELDS:
            raise ValueError(f"Campo não editável: {field}")
        old = before[field]
        if field == "owners":
            value = sorted(set(value or []))
        if (old or None) != (value or None):
            diff[field] = (old, value)
    if not diff:
        return {}
    for field, (_, value) in diff.items():
        if field == "owners":
            conn.execute("DELETE FROM activity_owners WHERE activity_id = ?", (activity_id,))
            for mid in value:
                conn.execute("INSERT INTO activity_owners (activity_id, member_id) VALUES (?,?)", (activity_id, mid))
        else:
            conn.execute(f"UPDATE activities SET {field} = ? WHERE activity_id = ?", (value, activity_id))
    conn.execute("UPDATE activities SET updated_at = ? WHERE activity_id = ?", (now_iso(), activity_id))
    after = snapshot(conn, activity_id)
    _log_event(conn, activity_id, actor_id, {k: before[k] for k in diff}, {k: after[k] for k in diff}, reason,
               source_file_id, source_version, suggestion_id)
    return diff


def add_ref(conn, activity_id, file_id, version, section, quote, relation):
    exists = conn.execute(
        """SELECT 1 FROM activity_refs WHERE activity_id=? AND file_id=? AND IFNULL(version_or_hash,'')=IFNULL(?, '')
           AND relation_type=? AND IFNULL(sheet_or_section,'')=IFNULL(?, '')""",
        (activity_id, file_id, version, relation, section)).fetchone()
    if exists:
        return
    conn.execute(
        """INSERT INTO activity_refs (activity_id, file_id, version_or_hash, sheet_or_section, quote_or_cell,
                                      relation_type, created_at) VALUES (?,?,?,?,?,?,?)""",
        (activity_id, file_id, version, section, quote, relation, now_iso()),
    )


# ---------------------------------------------------------------------------
# Consultas para a interface
# ---------------------------------------------------------------------------
def list_activities(conn: sqlite3.Connection, member_id: str | None = None, include_done: bool = False) -> list[dict]:
    sql = "SELECT a.* FROM activities a"
    params: list = []
    where = []
    if member_id:
        sql += " JOIN activity_owners o ON o.activity_id = a.activity_id"
        where.append("o.member_id = ?")
        params.append(member_id)
    if not include_done:
        where.append("a.status <> 'Concluída'")
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Prazo mais próximo primeiro; sem prazo ao final
    sql += " ORDER BY a.due_date IS NULL, a.due_date, a.activity_id"
    rows = [dict(r) for r in conn.execute(sql, params)]
    names = {r["member_id"]: r["display_name"] for r in conn.execute("SELECT * FROM members")}
    for r in rows:
        r["owners"] = get_owners(conn, r["activity_id"])
        r["owner_names"] = [names.get(m, m) for m in r["owners"]]
        r["pending_suggestions"] = conn.execute(
            "SELECT COUNT(*) FROM suggestions WHERE target_activity_id = ? AND review_status = 'pendente'",
            (r["activity_id"],)).fetchone()[0]
        r["last_movement"] = last_movement(conn, r["activity_id"]) or r["updated_at"]
        r["block_reason"] = block_reason(conn, r) if r["status"] == "Bloqueada" else None
    return rows


# ---------------------------------------------------------------------------
# Bloquear e reabrir
# ---------------------------------------------------------------------------
BLOCK_PREFIX = "Motivo do bloqueio: "


def _status_events(conn: sqlite3.Connection, activity_id: str):
    """Eventos que mudaram a situação, do mais recente ao mais antigo."""
    for r in conn.execute("SELECT reason, before_json, after_json FROM activity_events WHERE activity_id = ? "
                          "ORDER BY id DESC", (activity_id,)):
        after = json.loads(r["after_json"]) if r["after_json"] else {}
        if "status" in after:
            yield r, (json.loads(r["before_json"]) if r["before_json"] else {}), after


def block_reason(conn: sqlite3.Connection, a: dict) -> str | None:
    """Motivo do bloqueio atual. Bloqueio feito aqui: o motivo registrado no histórico (as notas ficam intactas).
    Bloqueio vindo da planilha ou de sugestão: as notas da atividade."""
    for r, _, after in _status_events(conn, a["activity_id"]):
        if after["status"] == "Bloqueada" and (r["reason"] or "").startswith(BLOCK_PREFIX):
            return r["reason"][len(BLOCK_PREFIX):]
        break
    return a.get("notes")


def last_block_reason(conn: sqlite3.Connection, activity_id: str) -> str | None:
    """Motivo do último bloqueio feito na Central (para reabrir uma atividade que estava bloqueada)."""
    for r, _, after in _status_events(conn, activity_id):
        if after["status"] == "Bloqueada" and (r["reason"] or "").startswith(BLOCK_PREFIX):
            return r["reason"][len(BLOCK_PREFIX):]
    return None


def status_before_done(conn: sqlite3.Connection, activity_id: str) -> str:
    """Situação que a atividade tinha antes de ser concluída (para "Reabrir"). Sem registro: "A fazer"."""
    for _, before, after in _status_events(conn, activity_id):
        if after["status"] == "Concluída":
            prev = (before or {}).get("status")
            return prev if prev in STATUSES and prev != "Concluída" else "A fazer"
        break
    return "A fazer"


# ---------------------------------------------------------------------------
# "Isso ainda está valendo?": atividade aberta há muito tempo sem nenhuma novidade
# ---------------------------------------------------------------------------
CONFIRM_REASON = "Confirmou que continua valendo"


def last_movement(conn: sqlite3.Connection, activity_id: str) -> str | None:
    """Última novidade da atividade: qualquer evento do histórico (mudança, sugestão aceita, confirmação)."""
    return conn.execute("SELECT MAX(ts) FROM activity_events WHERE activity_id = ?", (activity_id,)).fetchone()[0]


def days_without_news(a: dict, today: date, limit: int) -> int | None:
    """Dias sem novidade quando passou do limite; None se está em dia, concluída ou com sugestão chegando.
    limit 0 desliga a verificação."""
    if not limit or a.get("status") == "Concluída" or a.get("pending_suggestions"):
        return None
    last = a.get("last_movement") or a.get("updated_at")
    if not last:
        return None
    days = (today - date.fromisoformat(last[:10])).days
    return days if days >= limit else None


def confirm_still_valid(conn: sqlite3.Connection, activity_id: str, actor_id: str) -> None:
    """Registra no histórico que a atividade continua valendo. Nenhum campo muda; a contagem recomeça."""
    _log_event(conn, activity_id, actor_id, {}, {}, CONFIRM_REASON)


def activity_history(conn: sqlite3.Connection, activity_id: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM activity_events WHERE activity_id = ? ORDER BY id DESC", (activity_id,)):
        d = dict(r)
        d["before"] = json.loads(r["before_json"]) if r["before_json"] else None
        d["after"] = json.loads(r["after_json"]) if r["after_json"] else None
        out.append(d)
    return out
