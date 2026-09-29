"""Bot da Central no Discord: /pergunta, /prazos e /minhas.

- Só comandos de barra: o Discord entrega ao bot apenas o texto do comando. O bot não pede a permissão de
  ler as mensagens do canal (intent "Message Content"), então não vê as conversas da Liga.
- Só leitura: nada muda no quadro pelo Discord. Aceitar sugestão ou editar atividade continua no site.
- Conexão por dentro (gateway): funciona no computador local, sem endereço público.
- Respostas sem menções (@everyone, @pessoa) e sem prévia de links.
- /pergunta usa exatamente o mesmo `ask.ask` do site: trecho conferido no documento, quadro acima das atas,
  sugestão pendente marcada como não oficial. Limite de uma pergunta a cada 10 s por pessoa.
Sem DISCORD_BOT_TOKEN no .env, nada disso roda.

As funções de texto (`pergunta_text`, `prazos_text`, `minhas_text`) não dependem do Discord e são testadas
sem rede; `start`/`stop` cuidam da conexão.
"""
# Sem "from __future__ import annotations": o discord.py lê as anotações dos comandos (definidos dentro de start)
import asyncio
import logging
import sqlite3
import threading
import time
from datetime import date, timedelta

from . import activities as acts
from . import ask

log = logging.getLogger("central.discord")
MAX_LEN = 1900          # limite do Discord é 2000 caracteres
COOLDOWN = 10           # segundos entre perguntas da mesma pessoa
QUOTE_MAX = 280


# ---------------------------------------------------------------------------
# Textos (sem Discord)
# ---------------------------------------------------------------------------
def _fmt(iso: str | None, year: int | None = None) -> str:
    if not iso:
        return "a definir"
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}" if year and int(y) == year else f"{d}/{m}/{y}"


def _names(conn) -> dict[str, str]:
    return {r["member_id"]: r["display_name"] for r in conn.execute("SELECT member_id, display_name FROM members")}


def _who(ids, names) -> str:
    ns = [names.get(i, i) for i in ids or []]
    if not ns:
        return "responsável a confirmar"
    return " e ".join(ns) if len(ns) <= 2 else ", ".join(ns[:-1]) + " e " + ns[-1]


def _one_line(text: str, limit: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _fit(lines: list[str], tail: list[str]) -> str:
    """Junta as linhas sem passar do limite do Discord; o rodapé (links) sempre aparece."""
    out, size = [], sum(len(t) + 1 for t in tail)
    for ln in lines:
        if size + len(ln) + 1 > MAX_LEN:
            out.append("…")
            break
        out.append(ln)
        size += len(ln) + 1
    return "\n".join(out + tail)


def _doc_link(d: dict, base_url: str) -> str:
    if d.get("kind") == "quadro":
        return f"📄 Quadro de atividades · <{base_url}/atividades>"
    if d.get("kind") == "pendentes":
        return f"📄 Sugestões aguardando revisão (ainda não oficiais) · <{base_url}/sugestoes>"
    extra = " · documento substituído" if d.get("kind") == "historico" else ""
    if d.get("file_id"):
        return f"📄 {d['name']}{extra} · <{base_url}/fontes/{d['file_id']}>"
    return f"📄 {d['name']}{extra}"


def pergunta_text(result: dict, asker: str, base_url: str) -> str:
    """Resposta do /pergunta a partir do resultado de `ask.ask`."""
    base_url = base_url.rstrip("/")
    head = f"**{_one_line(asker, 40)} perguntou:** {_one_line(result.get('question', ''), 300)}"
    status = result.get("status")
    if status == "empty":
        return "Escreva a pergunta depois do comando, por exemplo: `/pergunta quando é a primeira oficina?`"
    if status == "ok":
        lines = [head, f"💬 {result['answer'].strip()}"]
        for c in result["citations"]:
            lines += [f"> “{_one_line(c['quote'], QUOTE_MAX)}”", _doc_link(c["doc"], base_url)]
        if result.get("warning"):
            lines.append(f"⚠️ {result['warning']}")
        return _fit(lines, ["-# Cada trecho acima foi conferido, letra por letra, no documento citado."])
    if status == "fallback":
        lines = [head, f"🔎 {result.get('reason') or 'A IA não respondeu agora.'} "
                       "Trechos dos documentos que têm as palavras da pergunta:"]
        for h in result.get("hits") or []:
            lines += [f"> {_one_line(h['quote'], QUOTE_MAX)}", _doc_link(h["doc"], base_url)]
        if not result.get("hits"):
            lines.append("Nenhum trecho encontrado.")
        return _fit(lines, ["-# Busca simples por palavras, sem IA. Confira no documento."])
    # not_found (inclusive quando a IA respondeu mas nenhum trecho citado existia nos documentos)
    return "\n".join([head, "🤷 Não encontrei isso nos documentos da pasta, então não vou chutar uma resposta.",
                      f"Veja o Comece aqui: <{base_url}/comece-aqui>"])


def prazos_text(conn: sqlite3.Connection, base_url: str, today: date, days: int = 7) -> str:
    """Atividades abertas vencidas ou que vencem nos próximos `days` dias."""
    base_url = base_url.rstrip("/")
    names = _names(conn)
    limit = (today + timedelta(days=days)).isoformat()
    items = [a for a in acts.list_activities(conn) if a["due_date"] and a["due_date"] <= limit]
    if not items:
        return "\n".join([f"📅 **Nada vence nos próximos {days} dias.**", f"🔗 Todas as atividades: <{base_url}/atividades>"])
    lines = [f"📅 **Prazos até {_fmt(limit, today.year)}**"]
    for a in items:
        d = a["due_date"]
        when = ("⚠️ **Vencida** (" + _fmt(d, today.year) + ")" if d < today.isoformat()
                else "**Hoje**" if d == today.isoformat() else "**Amanhã**" if d == (today + timedelta(days=1)).isoformat()
                else f"**{_fmt(d, today.year)}**")
        lines.append(f"• {when} · {a['title']} · {_who(a['owners'], names)} · {a['status']}")
    return _fit(lines, [f"🔗 Todas as atividades: <{base_url}/atividades>"])


def minhas_text(conn: sqlite3.Connection, member_id: str, base_url: str, today: date, stale_days: int = 0) -> str:
    """Atividades abertas de uma pessoa, na mesma ordem do site (prazo mais próximo primeiro)."""
    base_url = base_url.rstrip("/")
    names = _names(conn)
    who = names.get(member_id)
    if not who:
        return "Não conheço essa pessoa. Escolha um nome da lista do comando."
    items = acts.list_activities(conn, member_id)
    if not items:
        return f"📋 **{who} não tem atividades abertas.**"
    lines = [f"📋 **Atividades abertas de {who}** ({len(items)})"]
    for a in items:
        tags = [f"Prazo {_fmt(a['due_date'], today.year)}", a["status"]]
        if a["due_date"] and a["due_date"] < today.isoformat():
            tags[0] = f"⚠️ Vencida ({_fmt(a['due_date'], today.year)})"
        if a["pending_suggestions"]:
            tags.append("sugestão aguardando revisão")
        n = acts.days_without_news(a, today, stale_days)
        if n:
            tags.append(f"sem novidade há {n} {'dia' if n == 1 else 'dias'}")
        shared = [o for o in a["owners"] if o != member_id]
        if shared:
            tags.append(f"com {_who(shared, names)}")
        lines.append(f"• **{a['title']}** · " + " · ".join(tags))
        lines.append(f"  Próximo passo: {_one_line(a['next_step'] or 'não definido', 140).rstrip('.')}")
    return _fit(lines, [f"🔗 Ver no site: <{base_url}/>"])


class Cooldown:
    """Uma pergunta a cada `seconds` por pessoa (protege a cota da IA)."""

    def __init__(self, seconds: float = COOLDOWN, clock=time.monotonic):
        self.seconds, self.clock, self.last = seconds, clock, {}
        self.lock = threading.Lock()

    def wait_for(self, user_id) -> float:
        """0 se pode perguntar agora (e marca o horário); senão, quantos segundos faltam."""
        with self.lock:
            now = self.clock()
            left = self.seconds - (now - self.last.get(user_id, -1e9))
            if left > 0:
                return left
            self.last[user_id] = now
            return 0.0


# ---------------------------------------------------------------------------
# Conexão com o Discord
# ---------------------------------------------------------------------------
_state: dict = {"client": None, "tree": None, "loop": None, "thread": None, "status": "desligado", "error": None}


def status() -> dict:
    return {"status": _state["status"], "error": _state["error"],
            "name": str(_state["client"].user) if _state["client"] and _state["client"].user else None}


def start(settings, connect, make_qa_llm, today) -> None:
    """Liga o bot numa thread própria (com o seu próprio loop). Não bloqueia o site."""
    if not settings.discord_bot_token or _state["thread"]:
        return
    import discord
    from discord import app_commands

    intents = discord.Intents.none()
    intents.guilds = True   # só o necessário para os comandos de barra; nada de ler mensagens
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
    tree = app_commands.CommandTree(client)
    cooldown = Cooldown()
    base = settings.app_base_url
    guild = discord.Object(id=int(settings.discord_guild_id)) if settings.discord_guild_id else None

    def with_db(fn, *args):
        conn = connect(settings.database_path)
        try:
            return fn(conn, *args)
        finally:
            conn.close()

    c0 = connect(settings.database_path)
    member_choices = [app_commands.Choice(name=r["display_name"], value=r["member_id"])
                      for r in c0.execute("SELECT member_id, display_name FROM members ORDER BY display_name")][:25]
    c0.close()

    async def send(interaction, text: str, ephemeral: bool = False):
        await interaction.followup.send(text, ephemeral=ephemeral, suppress_embeds=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @tree.command(name="pergunta", description="Pergunte à Central: resposta com o trecho do documento de origem")
    @app_commands.describe(texto="Sua pergunta, em português")
    async def pergunta(interaction: discord.Interaction, texto: app_commands.Range[str, 3, ask.MAX_QUESTION]):
        left = cooldown.wait_for(interaction.user.id)
        if left:
            await interaction.response.send_message(f"Espere {int(left) + 1} s para perguntar de novo.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)   # a IA pode levar mais que os 3 s do Discord
        try:
            result = await asyncio.to_thread(with_db, lambda conn, q: ask.ask(conn, make_qa_llm(), q), texto)
            await send(interaction, pergunta_text(result, interaction.user.display_name, base))
        except Exception:
            log.exception("Falha no /pergunta")
            await send(interaction, "Não consegui responder agora. Tente de novo em instantes ou use o site.", True)

    @tree.command(name="prazos", description="O que vence nos próximos 7 dias (e o que já venceu)")
    async def prazos(interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        await send(interaction, await asyncio.to_thread(with_db, lambda conn: prazos_text(conn, base, today())))

    @tree.command(name="minhas", description="Atividades abertas de uma pessoa")
    @app_commands.describe(pessoa="De quem são as atividades")
    @app_commands.choices(pessoa=member_choices)
    async def minhas(interaction: discord.Interaction, pessoa: app_commands.Choice[str]):
        await interaction.response.defer(thinking=True)
        await send(interaction, await asyncio.to_thread(
            with_db, lambda conn: minhas_text(conn, pessoa.value, base, today(), settings.stale_days)))

    @client.event
    async def on_ready():
        try:
            if guild:   # comandos no servidor aparecem na hora; globais podem levar até 1 h
                tree.copy_global_to(guild=guild)
                await tree.sync(guild=guild)
            else:
                await tree.sync()
            _state.update(status="conectado", error=None)
            log.info("Bot do Discord conectado como %s", client.user)
        except Exception as e:
            _state.update(status="erro", error=f"Não consegui registrar os comandos: {e}")
            log.exception("Falha ao registrar comandos do Discord")

    def run():
        loop = asyncio.new_event_loop()
        _state["loop"] = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(client.start(settings.discord_bot_token))
        except discord.LoginFailure:
            _state.update(status="erro", error="Token do bot recusado pelo Discord. Confira DISCORD_BOT_TOKEN no .env.")
            log.error(_state["error"])
        except Exception as e:
            _state.update(status="erro", error=f"Conexão com o Discord falhou: {type(e).__name__}")
            log.exception("Bot do Discord parou")
        finally:
            loop.close()

    _state.update(client=client, tree=tree, status="conectando", error=None)
    _state["thread"] = threading.Thread(target=run, daemon=True, name="discord-bot")
    _state["thread"].start()


def stop() -> None:
    client, loop = _state["client"], _state["loop"]
    if client and loop and not loop.is_closed():
        try:
            asyncio.run_coroutine_threadsafe(client.close(), loop).result(timeout=5)
        except Exception:
            pass
    _state.update(client=None, tree=None, loop=None, thread=None, status="desligado")
