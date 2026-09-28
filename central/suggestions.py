"""Sugestões: propostas de mudança que ficam separadas do registro oficial até revisão.

Garantias:
- Idempotência: a mesma proposta (mesma fonte, versão e conteúdo) nunca é gravada duas vezes.
- Aprovar é uma transição única 'pendente' -> 'aceita'. Reabrir a página ou clicar
  duas vezes não reaplica nem cria tarefa duplicada.
- Se o valor oficial mudou depois que a sugestão foi criada, a aprovação é barrada
  até a pessoa revisora confirmar olhando o valor atual.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from . import activities as acts
from .db import dumps, now_iso, transaction


class ReviewError(Exception):
    pass


def _dedupe_key(kind, target, proposed, source_file_id, source_version) -> str:
    raw = dumps([kind, target, proposed, source_file_id, source_version])
    return hashlib.sha256(raw.encode()).hexdigest()


def create_suggestion(conn: sqlite3.Connection, *, kind: str, target_activity_id: str | None, proposed: dict,
                      evidence: str, reason: str, source_file_id: str, source_version: str,
                      doc_date: str | None = None, uncertainties: list[str] | None = None,
                      model: str | None = None) -> int | None:
    """Grava uma sugestão pendente. Retorna None se ela já existia (idempotente)."""
    key = _dedupe_key(kind, target_activity_id, proposed, source_file_id, source_version)
    if conn.execute("SELECT 1 FROM suggestions WHERE dedupe_key = ?", (key,)).fetchone():
        return None
    # A mesma mudança já está pendente vinda de outra fonte? Não duplica.
    for row in conn.execute(
            "SELECT proposed_fields FROM suggestions WHERE review_status='pendente' AND kind=? "
            "AND IFNULL(target_activity_id,'') = IFNULL(?, '')", (kind, target_activity_id)):
        if json.loads(row["proposed_fields"]) == proposed:
            return None
    current = None
    if target_activity_id:
        snap = acts.snapshot(conn, target_activity_id)
        current = {k: snap[k] for k in proposed if k in snap} if snap else None
    cur = conn.execute(
        """INSERT INTO suggestions (kind, target_activity_id, proposed_fields, current_fields, evidence, reason,
                                    doc_date, uncertainties, source_file_id, source_version, review_status,
                                    model, created_at, dedupe_key)
           VALUES (?,?,?,?,?,?,?,?,?,?, 'pendente', ?,?,?)""",
        (kind, target_activity_id, dumps(proposed), dumps(current) if current is not None else None, evidence,
         reason, doc_date, dumps(uncertainties or []), source_file_id, source_version, model, now_iso(), key))
    return cur.lastrowid


def mark_stale_for_file(conn: sqlite3.Connection, file_id: str, keep_version: str | None, note: str) -> int:
    """Marca como desatualizadas as sugestões pendentes de versões antigas de um arquivo."""
    cur = conn.execute(
        "UPDATE suggestions SET review_status='desatualizada', review_note=?, reviewed_at=? "
        "WHERE source_file_id=? AND review_status='pendente' AND source_version <> IFNULL(?, '')",
        (note, now_iso(), file_id, keep_version))
    return cur.rowcount


def get(conn: sqlite3.Connection, suggestion_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM suggestions WHERE suggestion_id = ?", (suggestion_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    raw = json.loads(r["proposed_fields"])
    order = ["title", "owners", "due_date", "status", "next_step", "front", "priority", "notes", "description"]
    d["proposed"] = {k: raw[k] for k in order if k in raw} | {k: v for k, v in raw.items() if k not in order}
    d["current"] = json.loads(r["current_fields"]) if r["current_fields"] else None
    d["uncertainties_list"] = json.loads(r["uncertainties"] or "[]")
    return d


def list_suggestions(conn: sqlite3.Connection, status: str | None = "pendente") -> list[dict]:
    sql = "SELECT suggestion_id FROM suggestions"
    params = []
    if status:
        sql += " WHERE review_status = ?"
        params.append(status)
    sql += " ORDER BY suggestion_id DESC"
    return [get(conn, r["suggestion_id"]) for r in conn.execute(sql, params)]


def _check_reviewer(conn, reviewer_id):
    m = conn.execute("SELECT * FROM members WHERE member_id = ?", (reviewer_id,)).fetchone()
    if not m or not m["can_review"]:
        raise ReviewError("Esta pessoa não tem permissão para revisar sugestões.")


def accept(conn: sqlite3.Connection, suggestion_id: int, reviewer_id: str, adjusted: dict | None = None,
           note: str | None = None, confirm_current_changed: bool = False) -> str:
    """Aceita (opcionalmente com ajustes). Retorna o ID da atividade criada/alterada."""
    _check_reviewer(conn, reviewer_id)
    with transaction(conn):
        s = get(conn, suggestion_id)
        if not s:
            raise ReviewError("Sugestão não encontrada.")
        if s["review_status"] != "pendente":
            raise ReviewError(f"Esta sugestão já foi revisada (estado: {s['review_status']}). Nada foi reaplicado.")
        fields = dict(s["proposed"])
        if adjusted:
            fields.update({k: v for k, v in adjusted.items() if k in acts.EDITABLE_FIELDS})
        final_status = "aceita_com_ajuste" if adjusted and fields != s["proposed"] else "aceita"
        reason = f"Sugestão #{suggestion_id} aceita por revisão" + (f": {note}" if note else "")

        if s["kind"] == "update":
            target = s["target_activity_id"]
            now = acts.snapshot(conn, target)
            if now is None:
                raise ReviewError(f"A atividade {target} não existe mais.")
            changed_since = {k: (v, now.get(k)) for k, v in (s["current"] or {}).items() if now.get(k) != v}
            if changed_since and not confirm_current_changed:
                raise ReviewError(
                    "O valor oficial mudou desde que a sugestão foi criada: "
                    + "; ".join(f"{acts.FIELD_LABELS.get(k, k)} era {a!r}, agora {b!r}" for k, (a, b) in changed_since.items())
                    + ". Confirme para aplicar mesmo assim.")
            acts.update_activity(conn, target, fields, actor_id=reviewer_id, reason=reason,
                                 source_file_id=s["source_file_id"], source_version=s["source_version"],
                                 suggestion_id=suggestion_id)
            acts.add_ref(conn, target, s["source_file_id"], s["source_version"], None, s["evidence"], "alterada_por")
        elif s["kind"] == "create":
            fields.setdefault("status", "A fazer")
            target = acts.create_activity(conn, fields, actor_id=reviewer_id, creation_kind="sugestao", reason=reason,
                                          source_file_id=s["source_file_id"], source_version=s["source_version"],
                                          suggestion_id=suggestion_id)
            acts.add_ref(conn, target, s["source_file_id"], s["source_version"], None, s["evidence"], "criada_por")
            conn.execute("UPDATE suggestions SET target_activity_id=? WHERE suggestion_id=?", (target, suggestion_id))
        else:
            raise ReviewError(f"Tipo de sugestão desconhecido: {s['kind']}")

        conn.execute(
            "UPDATE suggestions SET review_status=?, reviewer_id=?, reviewed_at=?, review_note=?, proposed_fields=? "
            "WHERE suggestion_id=? AND review_status='pendente'",
            (final_status, reviewer_id, now_iso(), note, dumps(fields) if final_status == "aceita_com_ajuste"
             else s["proposed_fields"], suggestion_id))
        return target


def reject(conn: sqlite3.Connection, suggestion_id: int, reviewer_id: str, reason: str) -> None:
    _check_reviewer(conn, reviewer_id)
    if not (reason or "").strip():
        raise ReviewError("Informe o motivo da rejeição.")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE suggestions SET review_status='rejeitada', reviewer_id=?, reviewed_at=?, review_note=? "
            "WHERE suggestion_id=? AND review_status='pendente'", (reviewer_id, now_iso(), reason.strip(), suggestion_id))
        if cur.rowcount == 0:
            raise ReviewError("Esta sugestão não está pendente; nada foi alterado.")
