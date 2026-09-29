"""Avisos no Discord: nova sugestão para revisar, sugestão decidida e prazo perto (amanhã) ou no dia.

Como funciona:
- `collect` olha o estado atual e enfileira o que ainda não foi avisado (chave única por aviso);
- `flush` manda a fila para o webhook do canal; falha fica na fila e é tentada de novo depois;
- na primeira vez (avisos recém-ligados), o que já existia é marcado como "visto" sem mandar nada,
  para não inundar o canal com o passado;
- menções (@everyone, @pessoa) são desligadas na mensagem: o texto vem de documentos e não pode marcar ninguém.
Sem DISCORD_WEBHOOK_URL no .env, nada disso roda.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, timedelta

from . import activities as acts
from .db import get_setting, now_iso, set_setting

log = logging.getLogger("central.notify")
MAX_ATTEMPTS = 8
MAX_LEN = 1900  # limite do Discord é 2000 caracteres


def valid_webhook(url: str | None) -> bool:
    return bool(url) and url.startswith(("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/",
                                         "https://ptb.discord.com/api/webhooks/", "https://canary.discord.com/api/webhooks/"))


def _fmt(iso: str | None) -> str:
    if not iso:
        return "a definir"
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}/{y}"


def _names(conn) -> dict[str, str]:
    return {r["member_id"]: r["display_name"] for r in conn.execute("SELECT member_id, display_name FROM members")}


def _who(ids, names) -> str:
    ns = [names.get(i, i) for i in ids or []]
    if not ns:
        return "responsável a confirmar"
    return " e ".join(ns) if len(ns) <= 2 else ", ".join(ns[:-1]) + " e " + ns[-1]


def _reviewers(conn) -> str:
    ns = [r["display_name"] for r in conn.execute("SELECT display_name FROM members WHERE can_review=1 ORDER BY display_name")]
    return " ou ".join(ns) if ns else "quem revisa"


def _enqueue(conn, key: str, kind: str, text: str, sent: str | None = None) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO notifications (key, kind, text, created_at, sent_at, attempts) "
                       "VALUES (?,?,?,?,?,0)", (key, kind, text[:MAX_LEN], now_iso(), sent))
    return cur.rowcount == 1


def _suggestion_title(s: dict, title: str | None) -> str:
    if s["kind"] == "update":
        fields = [acts.FIELD_LABELS.get(k, k).lower() for k in s["proposed"]]
        what = " e ".join(fields) if len(fields) <= 2 else ", ".join(fields[:-1]) + " e " + fields[-1]
        return f"Mudar {what}: *{title or s['target_activity_id']}*"
    return f"Nova atividade: *{s['proposed'].get('title') or 'sem título'}*"


def _new_suggestion_text(conn, s: dict, base_url: str, title: str | None) -> str:
    src = conn.execute("SELECT name FROM sources WHERE file_id=?", (s["source_file_id"],)).fetchone()
    ev = (s.get("evidence") or "").strip().replace("\n", " ")
    if len(ev) > 300:
        ev = ev[:297] + "…"
    return (f"**Nova sugestão para revisar** ({_reviewers(conn)})\n{_suggestion_title(s, title)}\n"
            f"> {ev}\n{src['name'] if src else 'Documento'}\nRevisar: {base_url}/sugestoes/{s['suggestion_id']}")


def _decided_text(conn, s: dict, base_url: str, title: str | None, names: dict) -> str:
    who_decided = names.get(s["reviewer_id"], s["reviewer_id"] or "alguém")
    target = s["target_activity_id"]
    owners = acts.get_owners(conn, target) if target else []
    head = _suggestion_title(s, title)
    if s["review_status"] == "rejeitada":
        return (f"**Sugestão rejeitada** por {who_decided}\n{head}\nMotivo: “{s['review_note'] or 'não informado'}”. "
                f"O quadro de atividades não mudou.\nVer: {base_url}/sugestoes/{s['suggestion_id']}")
    parts = []
    for k, v in s["proposed"].items():
        v = _fmt(v) if k == "due_date" else (_who(v, names) if k == "owners" else v)
        parts.append(f"{acts.FIELD_LABELS.get(k, k).lower()}: {v}")
    adj = " com ajuste" if s["review_status"] == "aceita_com_ajuste" else ""
    return (f"**Sugestão aceita{adj}** por {who_decided}\n{head}\nAgora vale: {'; '.join(parts)}.\n"
            f"Responsáveis: {_who(owners, names)}\nVer: {base_url}/atividades/{target}" if target else
            f"**Sugestão aceita{adj}** por {who_decided}\n{head}\nVer: {base_url}/sugestoes/{s['suggestion_id']}")


def _due_text(a: dict, when: str, base_url: str, names: dict) -> str:
    label = "Prazo amanhã" if when == "amanha" else "Prazo hoje"
    return (f"**{label}**: *{a['title']}* ({_who(a['owners'], names)}), {_fmt(a['due_date'])}.\n"
            f"Próximo passo: {a['next_step'] or 'não definido'}\nVer: {base_url}/atividades/{a['activity_id']}")


def collect(conn: sqlite3.Connection, base_url: str, today: date | None = None) -> int:
    """Enfileira os avisos que ainda não foram dados. Devolve quantos entraram na fila."""
    from . import suggestions as sugg
    today = today or date.today()
    base_url = base_url.rstrip("/")
    first_time = not get_setting(conn, "notify_baseline")
    baseline = "ja_existia" if first_time else None
    names = _names(conn)
    titles = {r["activity_id"]: r["title"] for r in conn.execute("SELECT activity_id, title FROM activities")}
    n = 0
    for s in sugg.list_suggestions(conn, None):
        title = titles.get(s["target_activity_id"])
        if s["review_status"] == "pendente":
            n += _enqueue(conn, f"sug_nova:{s['suggestion_id']}", "sugestao_nova",
                          _new_suggestion_text(conn, s, base_url, title), sent=baseline) and not baseline
        elif s["review_status"] in ("aceita", "aceita_com_ajuste", "rejeitada"):
            n += _enqueue(conn, f"sug_decidida:{s['suggestion_id']}", "sugestao_decidida",
                          _decided_text(conn, s, base_url, title, names), sent=baseline) and not baseline
    tomorrow = (today + timedelta(days=1)).isoformat()
    for a in acts.list_activities(conn):
        if a["due_date"] == tomorrow:
            n += _enqueue(conn, f"prazo_amanha:{a['activity_id']}:{a['due_date']}", "prazo",
                          _due_text(a, "amanha", base_url, names))
        elif a["due_date"] == today.isoformat():
            n += _enqueue(conn, f"prazo_hoje:{a['activity_id']}:{a['due_date']}", "prazo",
                          _due_text(a, "hoje", base_url, names))
    if first_time:
        set_setting(conn, "notify_baseline", now_iso())
    return n


def _post(url: str, text: str) -> tuple[bool, str | None]:
    import httpx
    try:
        r = httpx.post(url, json={"content": text, "username": "Central da Liga",
                                  "allowed_mentions": {"parse": []}}, timeout=10)
    except httpx.HTTPError as e:
        return False, f"falha de rede ({type(e).__name__})"
    if r.status_code in (200, 204):
        return True, None
    return False, f"HTTP {r.status_code}: {r.text[:150]}"


def flush(conn: sqlite3.Connection, url: str, sender=None, limit: int = 20) -> dict:
    """Manda os avisos pendentes, na ordem em que entraram."""
    sender = sender or _post
    sent = failed = 0
    rows = conn.execute("SELECT * FROM notifications WHERE sent_at IS NULL AND attempts < ? ORDER BY rowid LIMIT ?",
                        (MAX_ATTEMPTS, limit)).fetchall()
    for r in rows:
        ok, err = sender(url, r["text"])
        if ok:
            conn.execute("UPDATE notifications SET sent_at=?, last_error=NULL WHERE key=?", (now_iso(), r["key"]))
            sent += 1
        else:
            conn.execute("UPDATE notifications SET attempts=attempts+1, last_error=? WHERE key=?", (err, r["key"]))
            failed += 1
            log.warning("Aviso no Discord não enviado (%s): %s", r["key"], err)
            break  # Discord fora do ar ou limite de envio: tenta o resto na próxima rodada
    return {"sent": sent, "failed": failed}


def status(conn: sqlite3.Connection) -> dict:
    last = conn.execute("SELECT sent_at FROM notifications WHERE sent_at LIKE '20%' ORDER BY sent_at DESC LIMIT 1").fetchone()
    queued = conn.execute("SELECT COUNT(*) FROM notifications WHERE sent_at IS NULL AND attempts < ?", (MAX_ATTEMPTS,)).fetchone()[0]
    err = conn.execute("SELECT last_error FROM notifications WHERE sent_at IS NULL AND last_error IS NOT NULL "
                       "ORDER BY rowid DESC LIMIT 1").fetchone()
    return {"last_sent": last["sent_at"] if last else None, "queued": queued, "last_error": err["last_error"] if err else None}
