"""Bot da Central no Discord: textos das respostas e os comandos, com um Discord simulado (sem rede)."""
import asyncio
import re
from datetime import date
from types import SimpleNamespace

import pytest

from central import ask, discord_bot as bot
from central.ai import FakeLLM
from central.db import connect

BASE = "http://localhost:8000"


@pytest.fixture(autouse=True)
def _limpo():
    ask._cache.clear()
    yield
    ask._cache.clear()
    bot.stop()


def _responde_guia(system, user):
    doc = re.search(r"\[(D\d+)\] GUIA_INICIAL\.md", user).group(1)
    return {"found": True, "answer": "Bruno, líder de Growth.",
            "citations": [{"doc": doc, "quote": "Bruno é líder da frente e aprova posts antes de publicação."}],
            "warning": None}


# --------------------------------------------------------------------------- textos
def test_pergunta_com_trecho_e_link(conn, sync):
    sync()
    r = ask.ask(conn, FakeLLM(_responde_guia), "Quem aprova os posts de Growth?")
    t = bot.pergunta_text(r, "João", BASE)
    assert t.startswith("**João perguntou:** Quem aprova os posts de Growth?\n💬 Bruno, líder de Growth.")
    assert "> “Bruno é líder da frente e aprova posts antes de publicação.”" in t
    assert re.search(r"📄 GUIA_INICIAL\.md · <http://localhost:8000/fontes/[^>]+>", t)
    assert "conferido, letra por letra" in t


def test_pergunta_sem_resposta_nao_chuta(conn, sync):
    sync()
    inventada = FakeLLM(lambda s, u: {"found": True, "answer": "Carla aprova tudo.",
                                      "citations": [{"doc": "D1", "quote": "Carla aprova todos os posts."}]})
    t = bot.pergunta_text(ask.ask(conn, inventada, "Quem aprova os posts?"), "Ana", BASE)
    assert "Não encontrei isso nos documentos" in t and "Carla aprova tudo" not in t


def test_pergunta_sem_ia_cai_na_busca_simples(conn, sync):
    sync()
    t = bot.pergunta_text(ask.ask(conn, None, "Quem aprova os posts de Growth?"), "Ana", BASE)
    assert "A IA está desligada" in t and "Busca simples por palavras" in t


def test_resposta_nunca_passa_do_limite_do_discord():
    doc = {"name": "Ata.md", "kind": "ata", "file_id": "x"}
    r = {"status": "ok", "question": "q" * 300, "answer": "a" * 600,
         "citations": [{"doc": doc, "quote": "t" * 1000}] * 20, "warning": "w"}
    t = bot.pergunta_text(r, "João", BASE)
    assert len(t) <= 2000 and t.endswith("citado.")


def test_prazos(conn, sync):
    sync()
    t = bot.prazos_text(conn, BASE, today=date(2026, 10, 6))
    assert t.startswith("📅 **Prazos até 13/10**")
    assert "• ⚠️ **Vencida** (05/10) · Preparar carrossel sobre ferramentas · Ana" in t
    assert "• **Hoje** · Montar checklist inicial de onboarding · Davi · A fazer" in t
    assert t.endswith(f"<{BASE}/atividades>")
    vazio = bot.prazos_text(conn, BASE, today=date(2026, 1, 1))
    assert "Nada vence nos próximos 7 dias" in vazio


def test_minhas(conn, sync):
    sync()
    t = bot.minhas_text(conn, "U-D", BASE, today=date(2026, 10, 1))
    assert t.startswith("📋 **Atividades abertas de Davi** (2)")
    assert "• **Montar checklist inicial de onboarding** · Prazo 06/10 · A fazer" in t
    assert "com Ana" in t and "Próximo passo:" in t
    assert "Não conheço essa pessoa" in bot.minhas_text(conn, "U-X", BASE, today=date(2026, 10, 1))


def test_uma_pergunta_a_cada_10_segundos():
    agora = [100.0]
    c = bot.Cooldown(10, clock=lambda: agora[0])
    assert c.wait_for(1) == 0 and c.wait_for(2) == 0      # pessoas diferentes não se atrapalham
    assert c.wait_for(1) == 10
    agora[0] += 10
    assert c.wait_for(1) == 0


# --------------------------------------------------------------------------- comandos (Discord simulado)
class Interacao:
    def __init__(self, user_id=1, name="João"):
        self.user = SimpleNamespace(id=user_id, display_name=name)
        self.enviados, self.efemeros, self.adiado = [], [], False
        outer = self

        class Resp:
            async def defer(self, thinking=False):
                outer.adiado = True

            async def send_message(self, text, ephemeral=False):
                outer.efemeros.append(text)

        class Follow:
            async def send(self, text, **kw):
                outer.enviados.append((text, kw))

        self.response, self.followup = Resp(), Follow()


@pytest.fixture
def comandos(conn, sync, tmp_path, monkeypatch):
    sync()
    conn.commit()
    import discord

    async def sem_rede(self, token, *, reconnect=True):
        return None
    monkeypatch.setattr(discord.Client, "start", sem_rede)
    settings = SimpleNamespace(discord_bot_token="token-falso", discord_guild_id="", app_base_url=BASE,
                               database_path=tmp_path / "central.db", stale_days=14)
    bot.start(settings, connect, lambda: FakeLLM(_responde_guia), lambda: date(2026, 10, 6))
    bot._state["thread"].join(timeout=5)
    return {c.name: c for c in bot._state["tree"].get_commands()}


def test_comandos_registrados_sem_ler_mensagens(comandos):
    assert set(comandos) == {"pergunta", "prazos", "minhas"}
    assert not bot._state["client"].intents.message_content   # o bot não lê as conversas do canal
    assert [c.name for c in comandos["minhas"].parameters[0].choices] == ["Ana", "Bruno", "Carla", "Davi"]


def test_comando_pergunta_publico_sem_mencoes(comandos):
    i = Interacao()
    asyncio.run(comandos["pergunta"].callback(i, texto="Quem aprova os posts de Growth?"))
    texto, kw = i.enviados[0]
    assert i.adiado and texto.startswith("**João perguntou:**") and "Bruno é líder da frente" in texto
    assert kw["ephemeral"] is False and kw["suppress_embeds"] is True
    assert not kw["allowed_mentions"].everyone and not kw["allowed_mentions"].users
    i2 = Interacao()
    asyncio.run(comandos["pergunta"].callback(i2, texto="E quem revisa?"))
    assert i2.efemeros and i2.efemeros[0].startswith("Espere") and not i2.enviados


def test_comandos_prazos_e_minhas(comandos):
    i = Interacao()
    asyncio.run(comandos["prazos"].callback(i))
    assert "**Hoje** · Montar checklist inicial de onboarding" in i.enviados[0][0]
    i = Interacao()
    asyncio.run(comandos["minhas"].callback(i, pessoa=SimpleNamespace(value="U-A", name="Ana")))
    assert i.enviados[0][0].startswith("📋 **Atividades abertas de Ana**")
