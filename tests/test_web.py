"""Teste de fumaça da interface: as páginas abrem e o fluxo de revisão funciona via HTTP."""
import importlib
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from central.ai import FakeLLM
from central.db import connect, init_db
from central.sources import LocalSource
from central.sync import run_sync

from .conftest import DATA, responder_padrao


@pytest.fixture
def client(tmp_path, monkeypatch):
    folder = tmp_path / "drive"
    shutil.copytree(DATA / "01_CARGA_INICIAL", folder)
    db_path = tmp_path / "web.db"
    monkeypatch.setenv("SOURCE_MODE", "local")
    monkeypatch.setenv("LOCAL_FOLDER", str(folder))
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    monkeypatch.setenv("LLM_PROVIDER", "off")
    monkeypatch.setenv("SYNC_INTERVAL_SECONDS", "3600")
    conn = connect(db_path)
    init_db(conn)
    run_sync(conn, LocalSource(folder), llm=FakeLLM(responder_padrao))
    shutil.copy(DATA / "02_ADICIONAR_DEPOIS_DA_CARGA" / "Ata_2026-10-03.md", folder / "Ata_2026-10-03.gdoc")
    shutil.copy(DATA / "02_ADICIONAR_DEPOIS_DA_CARGA" / "Ata_2026-10-04.md", folder)
    shutil.copy(DATA / "03_CONFLITO" / "Ata - copia vazia.xlsx", folder)
    run_sync(conn, LocalSource(folder), llm=FakeLLM(responder_padrao))
    conn.close()
    import central.app as app_module
    app_module = importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c


def login(c, member_id):
    c.post("/entrar", data={"member_id": member_id})


def test_paginas_abrem(client):
    login(client, "U-A")
    for url in ["/", "/atividades", "/atividades/ACT-104", "/sugestoes", "/sugestoes?estado=todas", "/novidades",
                "/novidades?desde=30d", "/comece-aqui", "/fontes", "/pendencias", "/sincronizacao",
                "/atividades/nova", "/sugestoes/1"]:
        r = client.get(url)
        assert r.status_code == 200, url
    home = client.get("/").text
    assert "ACT-101" in home and "ACT-104" in home and "ACT-102" not in home


def test_revisao_pela_interface(client):
    import re
    sid = re.search(r'href="/sugestoes/(\d+)">Mudar [^<]*: Preparar carrossel sobre ferramentas', client.get("/sugestoes").text).group(1)
    login(client, "U-A")
    client.post(f"/sugestoes/{sid}/aceitar", data={})
    assert "05/10/2026" in client.get("/atividades/ACT-101").text  # Ana não pode aprovar
    login(client, "U-B")
    r = client.post(f"/sugestoes/{sid}/aceitar", data={}, follow_redirects=True)
    assert "07/10/2026" in r.text
    r = client.post(f"/sugestoes/{sid}/aceitar", data={}, follow_redirects=True)
    assert "já foi revisada" in r.text


def test_criar_atividade_valida_campos(client):
    login(client, "U-D")
    r = client.post("/atividades/nova", data={"title": "", "status": "A fazer"})
    assert "Informe um título" in r.text
    r = client.post("/atividades/nova", data={"title": "Nova tarefa", "status": "A fazer", "owners": ["U-D"]},
                    follow_redirects=True)
    assert "Nova tarefa" in r.text and "ACT-105" in r.text


def test_painel_de_decisao(client):
    """Confirmação só aparece se o oficial mudou; depois de decidir, a página oferece a próxima sugestão."""
    import re
    login(client, "U-B")
    lista = client.get("/sugestoes").text
    sid = re.search(r'href="/sugestoes/(\d+)">Mudar [^<]*: Preparar carrossel sobre ferramentas', lista).group(1)
    pagina = client.get(f"/sugestoes/{sid}").text
    assert 'name="confirmar"' not in pagina
    assert "Ao aceitar, <strong>Preparar carrossel sobre ferramentas</strong> passa a ter" in pagina
    # alguém muda o prazo oficial à mão depois da sugestão
    client.post("/atividades/ACT-101/editar", data={"title": "Preparar carrossel sobre ferramentas", "status": "Em andamento",
                                                     "due_date": "2026-10-20", "owners": ["U-A"], "reason": "teste"})
    pagina = client.get(f"/sugestoes/{sid}").text
    assert 'name="confirmar"' in pagina and "O valor oficial mudou depois desta sugestão" in pagina
    r = client.post(f"/sugestoes/{sid}/aceitar", data={"confirmar": "1"}, follow_redirects=True)
    assert "Sugestão aceita" in r.text
    assert "Próxima sugestão" in r.text or "Não há mais sugestões pendentes" in r.text


def test_pergunta_pela_interface(client):
    r = client.get("/comece-aqui?pergunta=Quem aprova os posts de Growth?")
    assert r.status_code == 200 and "Pergunte à Central" in r.text
    assert "IA está desligada" in r.text  # nos testes a IA fica desligada: cai na busca simples


def test_avisos_desligados_sem_webhook(client):
    assert "Avisos no Discord" in client.get("/sincronizacao").text
    r = client.post("/avisos/teste", follow_redirects=True)
    assert "DISCORD_WEBHOOK_URL" in r.text
