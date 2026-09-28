import shutil
from pathlib import Path

import pytest

from central.ai import FakeLLM
from central.db import connect, init_db
from central.sources import LocalSource
from central.sync import run_sync

DATA = Path(__file__).resolve().parent / "dados"


@pytest.fixture
def folder(tmp_path):
    """Pasta 'Drive' local com a carga inicial do pacote de testes."""
    root = tmp_path / "drive"
    shutil.copytree(DATA / "01_CARGA_INICIAL", root)
    return root


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "central.db")
    init_db(c)
    return c


def responder_padrao(system, user):
    """Simula o comportamento esperado de um bom modelo para as atas do pacote."""
    if "mudou de 2026-10-05 para" in user:
        return {"items": [{
            "kind": "update", "target_activity_id": "ACT-101", "due_date": "2026-10-07",
            "next_step": "Fechar o roteiro e enviar para Bruno", "owners": ["U-A"],
            "evidence": "O prazo para entregar a versão de aprovação mudou de 2026-10-05 para 2026-10-07.",
            "reason": "Decisão registrada na ata", "uncertainties": []}]}
    if "Carla revisará a pauta" in user:
        return {"items": [
            {"kind": "create", "target_activity_id": None, "title": "Propor exercício prático da primeira oficina",
             "owners": ["U-C"], "due_date": "2026-10-10", "next_step": "Escolher um problema real simples",
             "evidence": "Carla revisará a pauta da primeira oficina e entregará uma proposta de exercício prático até 2026-10-10.",
             "reason": "Nova tarefa assumida", "uncertainties": []},
            {"kind": "no_action", "evidence": "Talvez possamos publicar uma série diária de notícias sobre IA.",
             "reason": "Hipótese sem dono nem decisão", "uncertainties": []}]}
    if "ACT-102" in user and "Davi montará" in user:
        # Ata da carga inicial: o modelo repete o que já está no registro
        return {"items": [{"kind": "update", "target_activity_id": "ACT-101", "due_date": "2026-10-05",
                           "evidence": "A versão para aprovação deverá estar pronta até 2026-10-05.",
                           "reason": "Prazo registrado", "uncertainties": []}]}
    return {"items": []}


@pytest.fixture
def llm():
    return FakeLLM(responder_padrao)


@pytest.fixture
def sync(conn, folder, llm):
    def _sync(**kw):
        return run_sync(conn, LocalSource(folder), trigger="teste", llm=kw.get("llm", llm))
    return _sync
