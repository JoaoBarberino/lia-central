"""Avisos no Discord: nova sugestão para revisar, sugestão decidida, prazo perto (amanhã) ou no dia
e atividade parada ("Isso ainda está valendo?").

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


def _fmt(iso: str | None, year: int | None = None) -> str:
    """Data no formato 08/10; o ano só aparece quando não é o ano corrente."""
    if not iso:
        return "a definir"
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}" if year and int(y) == year else f"{d}/{m}/{y}"


def _clean(text) -> str:
    """Primeira letra maiúscula e sem pontuação no fim (a mensagem monta a própria pontuação)."""
    t = " ".join(str(text or "").split()).rstrip(" .;,")
    return t[:1].upper() + t[1:]


def _link(label: str, url: str) -> str:
    # <url> faz o Discord mostrar o link sem a prévia grande embaixo da mensagem
    return f"🔗 {label}: <{url}>"


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


SHORT_FIELDS = ("due_date", "owners", "status", "priority", "front")  # nesses vale mostrar antes → depois
ORDER = ["title", "owners", "due_date", "status", "next_step", "front", "priority", "notes", "description"]


def _value(k: str, v, names: dict, year: int | None) -> str:
    if k == "due_date":
        return _fmt(v, year)
    if k == "owners":
        return _who(v, names)
    return _clean(v) or "—"


def _change_lines(s: dict, names: dict, year: int | None) -> list[str]:
    """Uma linha por campo: '• Prazo: 06/10 → **08/10**'. Em atividade nova, título e responsáveis já estão no topo."""
    before = s.get("current") or {}
    lines = []
    for k in sorted(s["proposed"], key=lambda k: ORDER.index(k) if k in ORDER else len(ORDER)):
        if s["kind"] != "update" and k in ("title", "owners"):
            continue
        new = _value(k, s["proposed"][k], names, year)
        label = acts.FIELD_LABELS.get(k, k)
        if k in SHORT_FIELDS and k in before and before[k] != s["proposed"][k]:
            lines.append(f"• {label}: {_value(k, before[k], names, year)} → **{new}**")
        else:
            lines.append(f"• {label}: {new}")
    return lines


def _subject(conn, s: dict, title: str | None, names: dict, after: bool) -> str:
    """'**Título** · Responsáveis' da atividade de que a mensagem fala."""
    if s["kind"] == "update":
        target = s["target_activity_id"]
        owners = acts.get_owners(conn, target) if target else []
        if not after and "owners" in (s.get("current") or {}):
            owners = s["current"]["owners"]
        return f"**{title or target}** · {_who(owners, names)}"
    return f"**{_clean(s['proposed'].get('title')) or 'Sem título'}** · {_who(s['proposed'].get('owners'), names)}"


def _new_suggestion_text(conn, s: dict, base_url: str, title: str | None, names: dict, year: int | None) -> str:
    src = conn.execute("SELECT name, role FROM sources WHERE file_id=?", (s["source_file_id"],)).fetchone()
    ev = " ".join((s.get("evidence") or "").split())
    if len(ev) > 300:
        ev = ev[:297] + "…"
    lines = [f"📝 **Nova sugestão para revisar** ({_reviewers(conn)})",
             _subject(conn, s, title, names, after=False), *_change_lines(s, names, year)]
    if src:
        lines.append(f"{'Da ata' if src['role'] == 'ata' else 'Do documento'}: {src['name']}")
    if ev:
        lines.append(f"> \u201c{ev}\u201d")
    lines.append(_link("Revisar", f"{base_url}/sugestoes/{s['suggestion_id']}"))
    return "\n".join(lines)


def _decided_text(conn, s: dict, base_url: str, title: str | None, names: dict, year: int | None) -> str:
    who = names.get(s["reviewer_id"], s["reviewer_id"] or "Alguém")
    target = s["target_activity_id"]
    if s["review_status"] == "rejeitada":
        return "\n".join([
            f"❌ **{who} rejeitou uma sugestão**",
            f"**{title or target}**" if s["kind"] == "update"
            else f"**{_clean(s['proposed'].get('title')) or 'Sem título'}** (atividade nova)",
            f"Motivo: \u201c{_clean(s['review_note']) or 'Não informado'}\u201d",
            "O quadro de atividades não mudou.",
            _link("Ver sugestão", f"{base_url}/sugestoes/{s['suggestion_id']}")])
    what = "uma mudança" if s["kind"] == "update" else "uma atividade nova"
    adj = " (com ajuste)" if s["review_status"] == "aceita_com_ajuste" else ""
    link = (_link("Abrir atividade", f"{base_url}/atividades/{target}") if target
            else _link("Ver sugestão", f"{base_url}/sugestoes/{s['suggestion_id']}"))
    return "\n".join([f"✅ **{who} aceitou {what}{adj}**", _subject(conn, s, title, names, after=True),
                      *_change_lines(s, names, year), link])


def _due_text(a: dict, when: str, base_url: str, names: dict, year: int | None) -> str:
    label = "Prazo amanhã" if when == "amanha" else "Prazo hoje"
    return "\n".join([f"⏰ **{label} ({_fmt(a['due_date'], year)})**",
                      f"**{a['title']}** · {_who(a['owners'], names)}",
                      f"Próximo passo: {_clean(a['next_step']) or 'Não definido'}",
                      _link("Abrir atividade", f"{base_url}/atividades/{a['activity_id']}")])


def _stale_text(a: dict, days: int, base_url: str, names: dict, year: int | None) -> str:
    return "\n".join([
        "🕰️ **Isso ainda está valendo?**",
        f"**{a['title']}** · {_who(a['owners'], names)}",
        f"Sem novidade há {days} dias · Prazo {_fmt(a['due_date'], year)} · {a['status']}",
        _link("Confirmar ou atualizar", f"{base_url}/atividades/{a['activity_id']}")])


def collect(conn: sqlite3.Connection, base_url: str, today: date | None = None, stale_days: int = 0) -> int:
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
                          _new_suggestion_text(conn, s, base_url, title, names, today.year), sent=baseline) and not baseline
        elif s["review_status"] in ("aceita", "aceita_com_ajuste", "rejeitada"):
            n += _enqueue(conn, f"sug_decidida:{s['suggestion_id']}", "sugestao_decidida",
                          _decided_text(conn, s, base_url, title, names, today.year), sent=baseline) and not baseline
    tomorrow = (today + timedelta(days=1)).isoformat()
    for a in acts.list_activities(conn):
        if a["due_date"] == tomorrow:
            n += _enqueue(conn, f"prazo_amanha:{a['activity_id']}:{a['due_date']}", "prazo",
                          _due_text(a, "amanha", base_url, names, today.year))
        elif a["due_date"] == today.isoformat():
            n += _enqueue(conn, f"prazo_hoje:{a['activity_id']}:{a['due_date']}", "prazo",
                          _due_text(a, "hoje", base_url, names, today.year))
        # "Isso ainda está valendo?": uma vez por período parado (a chave muda quando a atividade tem novidade)
        days = acts.days_without_news(a, today, stale_days)
        if days:
            n += _enqueue(conn, f"parada:{a['activity_id']}:{a['last_movement'][:19]}", "parada",
                          _stale_text(a, days, base_url, names, today.year))
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
