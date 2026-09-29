"""Avisos no Discord: uma vez por evento, sem inundar ao ligar, sem menções e sem perder aviso se o Discord cair."""
from datetime import date

from central import notify
from central import suggestions as sugg

from .test_cenarios import add_file, pending

URL = "https://discord.com/api/webhooks/123/abc"
BASE = "http://localhost:8000"
HOJE = date(2026, 10, 1)


class Discord:
    def __init__(self, fora_do_ar=False):
        self.msgs, self.fora_do_ar = [], fora_do_ar

    def __call__(self, url, text):
        if self.fora_do_ar:
            return False, "HTTP 503"
        self.msgs.append(text)
        return True, None


def _rodada(conn, discord, today=HOJE):
    notify.collect(conn, BASE, today=today)
    return notify.flush(conn, URL, sender=discord)


def test_ao_ligar_nao_manda_o_passado(conn, sync, folder):
    sync()
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-03.md", as_gdoc=True)
    sync()
    d = Discord()
    _rodada(conn, d)
    assert d.msgs == []  # a sugestão já existia quando os avisos foram ligados


def test_nova_sugestao_e_decisao_avisam_uma_vez(conn, sync, folder):
    sync()
    d = Discord()
    _rodada(conn, d)                                   # liga os avisos
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-03.md", as_gdoc=True)
    sync()
    _rodada(conn, d)
    assert len(d.msgs) == 1 and d.msgs[0].startswith("📝 **Nova sugestão para revisar** (Bruno ou Carla)")
    assert "**Preparar carrossel sobre ferramentas** · Ana" in d.msgs[0]
    assert "• Prazo: 05/10 → **07/10**" in d.msgs[0] and f"<{BASE}/sugestoes/" in d.msgs[0]
    _rodada(conn, d)
    assert len(d.msgs) == 1                            # sincronizar de novo não repete
    sugg.accept(conn, pending(conn)[0]["suggestion_id"], "U-B")
    _rodada(conn, d)
    aceita = d.msgs[-1]
    assert aceita.startswith("✅ **Bruno aceitou uma mudança**\n**Preparar carrossel sobre ferramentas** · Ana")
    assert "• Prazo: 05/10 → **07/10**" in aceita and f"<{BASE}/atividades/ACT-101>" in aceita
    assert ".." not in aceita


def test_rejeicao_leva_o_motivo(conn, sync, folder):
    sync()
    d = Discord()
    _rodada(conn, d)
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-04.md")
    sync()
    sugg.reject(conn, pending(conn)[0]["suggestion_id"], "U-C", "Já está no plano da oficina")
    _rodada(conn, d)
    assert d.msgs[-1].startswith("❌ **Carla rejeitou uma sugestão**")
    assert "Motivo: \u201cJá está no plano da oficina\u201d" in d.msgs[-1] and "não mudou" in d.msgs[-1]


def test_prazo_amanha_e_hoje(conn, sync):
    sync()
    d = Discord()
    _rodada(conn, d, today=date(2026, 10, 5))          # ACT-102 vence 06/10; ACT-101 vence hoje (05/10)
    textos = "\n".join(d.msgs)
    assert "⏰ **Prazo amanhã (06/10)**\n**Montar checklist inicial de onboarding** · Davi" in textos
    assert "⏰ **Prazo hoje (05/10)**\n**Preparar carrossel sobre ferramentas** · Ana" in textos
    _rodada(conn, d, today=date(2026, 10, 5))
    assert len(d.msgs) == 2                            # no mesmo dia, uma vez só


def test_discord_fora_do_ar_nao_perde_aviso(conn, sync, folder):
    sync()
    _rodada(conn, Discord())
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-03.md", as_gdoc=True)
    sync()
    r = _rodada(conn, Discord(fora_do_ar=True))
    assert r["failed"] == 1 and notify.status(conn)["queued"] == 1
    d = Discord()
    _rodada(conn, d)
    assert len(d.msgs) == 1 and notify.status(conn)["queued"] == 0


def test_mensagem_nao_marca_ninguem(monkeypatch):
    enviado = {}

    class R:
        status_code = 204
        text = ""

    def fake_post(url, json, timeout):
        enviado.update(json)
        return R()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    ok, _ = notify._post(URL, "@everyone olha isso")
    assert ok and enviado["allowed_mentions"] == {"parse": []}


def test_texto_limpo_e_data_sem_ano_corrente():
    assert notify._clean("validar as etapas com Bruno.") == "Validar as etapas com Bruno"
    assert notify._fmt("2026-10-08", 2026) == "08/10" and notify._fmt("2027-01-02", 2026) == "02/01/2027"


def test_endereco_de_webhook_validado():
    assert notify.valid_webhook(URL)
    assert not notify.valid_webhook("https://exemplo.com/webhook") and not notify.valid_webhook("")
