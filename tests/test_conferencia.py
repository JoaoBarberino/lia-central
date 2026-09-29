"""'Isso ainda está valendo?': atividade aberta sem novidade há dias pede confirmação ao responsável."""
import os
from datetime import date

from central import activities as acts
from central import views
from central.db import connect

from .test_cenarios import add_file
from .test_web import client, login  # noqa: F401  (fixture)

DIA = date(2026, 1, 20)  # 19 dias depois de 01/01, data para onde o histórico é "envelhecido"


def envelhecer(conn):
    """Faz de conta que tudo aconteceu em 01/01/2026 (o teste não espera 14 dias de verdade)."""
    conn.execute("UPDATE activity_events SET ts = '2026-01-01T10:00:00-03:00'")
    conn.execute("UPDATE activities SET updated_at = '2026-01-01T10:00:00-03:00'")


def parada(conn, activity_id, today=DIA, limit=14):
    a = next(x for x in acts.list_activities(conn, include_done=True) if x["activity_id"] == activity_id)
    return acts.days_without_news(a, today, limit)


def test_regra_dos_dias(conn, sync, folder):
    sync()
    envelhecer(conn)
    assert parada(conn, "ACT-102") == 19
    assert parada(conn, "ACT-102", today=date(2026, 1, 14)) is None      # 13 dias: ainda em dia
    assert parada(conn, "ACT-102", limit=0) is None                      # 0 desliga
    acts.update_activity(conn, "ACT-104", {"status": "Concluída"}, actor_id="U-A", reason="teste")
    envelhecer(conn)
    assert parada(conn, "ACT-104") is None                               # concluída não é cobrada


def test_sugestao_chegando_nao_conta_como_parada(conn, sync, folder):
    sync()
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-03.md", as_gdoc=True)
    sync()
    envelhecer(conn)
    assert parada(conn, "ACT-101") is None   # tem sugestão aguardando revisão: já há novidade sobre ela
    assert parada(conn, "ACT-102") == 19


def test_confirmar_recomeca_a_contagem_e_fica_no_historico(conn, sync):
    sync()
    envelhecer(conn)
    acts.confirm_still_valid(conn, "ACT-102", "U-D")
    assert parada(conn, "ACT-102", today=views.today()) is None
    assert acts.activity_history(conn, "ACT-102")[0]["reason"] == acts.CONFIRM_REASON
    assert acts.snapshot(conn, "ACT-102")["status"] == "A fazer"         # nada no quadro mudou


def _db():
    return connect(os.environ["DATABASE_PATH"])


def _envelhecer_site():
    c = connect(os.environ["DATABASE_PATH"])
    envelhecer(c)
    c.commit()
    c.close()


def test_pela_interface(client, monkeypatch):  # noqa: F811
    _envelhecer_site()
    monkeypatch.setattr(views, "today", lambda: DIA)
    login(client, "U-D")
    home = client.get("/").text
    assert "Isso ainda está valendo?" in home and "Sem novidade há 19 dias" in home
    assert "Sem novidade há 19 dias" in client.get("/atividades").text

    # Ana não é responsável pela ACT-102 nem aprova: não pode responder por Davi
    login(client, "U-A")
    r = client.post("/atividades/ACT-102/conferir", data={"resposta": "continua"})
    assert "Só os responsáveis" in r.text

    login(client, "U-D")
    client.post("/atividades/ACT-102/conferir", data={"resposta": "continua", "volta": "/"})
    monkeypatch.setattr(views, "today", lambda: date.today())
    pagina = client.get("/atividades/ACT-102").text
    assert "Isso ainda está valendo?" not in pagina and "Confirmou que continua valendo" in pagina

    monkeypatch.setattr(views, "today", lambda: DIA)
    client.post("/atividades/ACT-104/conferir", data={"resposta": "terminou"})
    assert acts.snapshot(_db(), "ACT-104")["status"] == "Concluída"

    # Bruno aprova sugestões: pode responder por qualquer atividade
    login(client, "U-B")
    assert "Continua valendo" in client.get("/atividades/ACT-103").text
