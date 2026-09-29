"""Busca e filtros: sem acento e sem maiúscula, todas as palavras, filtros combináveis e na URL."""
from datetime import date

from central import activities as acts
from central import busca, views

from .test_cenarios import add_file
from .test_web import client, login  # noqa: F401  (fixture)

HOJE = date(2026, 10, 6)


def _f(conn, **kw):
    items = acts.list_activities(conn, include_done=True)
    return [a["activity_id"] for a in busca.filter_activities(items, today=HOJE, stale_days=14,
                                                              names=views.member_names(conn), **kw)]


def test_filtros_do_case(conn, sync):
    sync()
    assert _f(conn, responsavel="U-D") == ["ACT-102", "ACT-104"]           # ACT-104 compartilhada conta
    assert _f(conn, frente="Formação") == ["ACT-103"]
    assert _f(conn, situacao="Bloqueada") == ["ACT-103"]
    assert _f(conn, prazo="vencidas") == ["ACT-101"]                       # 05/10 < 06/10
    assert _f(conn, prazo="7dias") == ["ACT-102", "ACT-103", "ACT-104"]
    assert _f(conn, responsavel="U-A", prazo="vencidas") == ["ACT-101"]    # combinados
    assert _f(conn, responsavel=busca.SEM) == []


def test_busca_sem_acento_todas_as_palavras(conn, sync):
    sync()
    assert _f(conn, q="ONBOARDING") == ["ACT-102"]
    assert _f(conn, q="formacao briefing") == ["ACT-103"]                 # frente sem acento + título
    assert _f(conn, q="davi") == ["ACT-102", "ACT-104"]                    # nome do responsável
    assert _f(conn, q="act-101") == ["ACT-101"]
    assert _f(conn, q="carrossel onboarding") == []                       # todas as palavras


def test_concluidas_so_quando_pedido(conn, sync):
    sync()
    acts.update_activity(conn, "ACT-104", {"status": "Concluída"}, actor_id="U-A", reason="teste")
    assert "ACT-104" not in _f(conn)
    assert "ACT-104" in _f(conn, situacao="todas") and _f(conn, situacao="Concluída") == ["ACT-104"]


def test_documentos_por_conteudo_com_trecho(conn, sync, folder):
    sync()
    add_file(folder, "04_EXTRAS", "Ata_2026-10-07.md")
    sync()
    rows = [dict(r) for r in conn.execute("SELECT * FROM sources")]
    achados = busca.filter_sources(conn, rows, q="sala oficina")
    ata = next(r for r in achados if r["name"] == "Ata_2026-10-07.md")
    assert ata["hit"]["match"].lower() == "sala" and "oficina" in ata["hit"]["after"]
    assert {r["name"] for r in busca.filter_sources(conn, rows, tipo="atas")} >= {"Ata_2026-10-01.md", "Ata_2026-10-07.md"}
    assert all(r["role"] == "historico" for r in busca.filter_sources(conn, rows, tipo="antigos"))
    assert busca.filter_sources(conn, rows, q="xyzinexistente") == []


def test_telas(client):  # noqa: F811
    login(client, "U-A")
    t = client.get("/atividades?frente=Growth&q=carrossel").text
    assert "1 atividade encontrada" in t and "Limpar filtros" in t and "Preparar carrossel" in t
    assert 'value="Growth" selected' in t or ">Growth</option>" in t
    assert "Nenhuma atividade com esses filtros." in client.get("/atividades?q=zzz").text
    home = client.get("/?q=revisar").text
    assert "1 atividade encontrada" in home and "Revisar fluxo" in home and "Preparar carrossel" not in home
    docs = client.get("/fontes?q=oficina").text
    assert "<mark>" in docs and "Limpar filtros" in docs
    assert client.get("/atividades?concluidas=1").status_code == 200    # link antigo continua valendo
