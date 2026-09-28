"""Pendências humanas: tudo que o sistema não pode resolver sozinho com segurança."""
from __future__ import annotations

import sqlite3

from .db import now_iso

ISSUE_LABELS = {
    "registro_homonimo": "Planilha concorrente ao registro oficial",
    "registro_nao_definido": "Registro oficial não definido",
    "registro_alterado": "Mudança no registro oficial precisa de revisão",
    "fonte_indisponivel": "Fonte indisponível",
    "erro_leitura": "Erro ao ler arquivo",
    "responsavel_desconhecido": "Responsável não reconhecido",
}


def open_issue(conn: sqlite3.Connection, kind: str, title: str, detail: str, *, dedupe_key: str,
               file_id: str | None = None) -> int:
    """Abre (ou reabre) uma pendência. A mesma dedupe_key nunca gera duas linhas."""
    row = conn.execute("SELECT issue_id, status FROM issues WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    if row:
        if row["status"] != "aberta":
            conn.execute(
                "UPDATE issues SET status='aberta', title=?, detail=?, resolved_at=NULL, resolved_by=NULL, "
                "resolution=NULL WHERE issue_id=?", (title, detail, row["issue_id"]))
        else:
            conn.execute("UPDATE issues SET title=?, detail=? WHERE issue_id=?", (title, detail, row["issue_id"]))
        return row["issue_id"]
    cur = conn.execute(
        "INSERT INTO issues (kind, file_id, title, detail, status, created_at, dedupe_key) VALUES (?,?,?,?,?,?,?)",
        (kind, file_id, title, detail, "aberta", now_iso(), dedupe_key))
    return cur.lastrowid


def resolve_issue(conn: sqlite3.Connection, dedupe_key: str | None = None, *, issue_id: int | None = None,
                  by: str = "sistema", resolution: str = "Resolvida automaticamente") -> None:
    if issue_id is not None:
        where, arg = "issue_id = ?", issue_id
    else:
        where, arg = "dedupe_key = ?", dedupe_key
    conn.execute(
        f"UPDATE issues SET status='resolvida', resolved_at=?, resolved_by=?, resolution=? "
        f"WHERE {where} AND status='aberta'", (now_iso(), by, resolution, arg))


def open_issues(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM issues WHERE status='aberta' ORDER BY issue_id DESC")]
