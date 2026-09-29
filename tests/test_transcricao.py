"""Transcrever com IA: só quando alguém pede, e só vale depois de conferido por uma pessoa."""
import os
import shutil

import pytest

from central import transcribe
from central.ai import FakeLLM, LLMError
from central.sources import LocalSource
from central.sync import run_sync

from .conftest import DATA, responder_padrao
from .test_cenarios import pending
from .test_web import client, login  # noqa: F401  (fixture)

SCAN = "Ata_2026-10-08_digitalizada.pdf"
LIDO = "Ata de reunião de 8 de outubro de 2026\n(documento digitalizado)\nCarla confirmou a sala da oficina."


def ia_que_le(system, user):
    """Faz o papel do Gemini: transcreve o scan e, na análise da ata, sugere desbloquear ACT-103."""
    if "Transcreva o arquivo anexado" in user:
        return {"text": LIDO, "legivel": True, "observacao": None}
    if "Carla confirmou a sala da oficina" in user:
        return {"items": [{"kind": "update", "target_activity_id": "ACT-103", "status": "Em andamento",
                           "evidence": "Carla confirmou a sala da oficina.",
                           "reason": "O bloqueio era a confirmação do espaço", "uncertainties": []}]}
    return responder_padrao(system, user)


@pytest.fixture
def com_scan(conn, folder):
    llm = FakeLLM(ia_que_le)
    src = LocalSource(folder)
    run_sync(conn, src, llm=llm)
    shutil.copy(DATA / "04_EXTRAS" / SCAN, folder)
    run_sync(conn, src, llm=llm)
    fid = conn.execute("SELECT file_id FROM sources WHERE name=?", (SCAN,)).fetchone()[0]
    return {"llm": llm, "src": src, "fid": fid}


def _row(conn, fid):
    return conn.execute("SELECT * FROM sources WHERE file_id=?", (fid,)).fetchone()


def test_scan_fica_nao_processado_ate_alguem_pedir(conn, com_scan):
    s = _row(conn, com_scan["fid"])
    assert s["sync_status"] == "nao_suportado" and transcribe.can_transcribe(s)
    assert transcribe.kind_label(s) == "PDF escaneado"
    assert not any("Transcreva" in u for _, u in com_scan["llm"].calls)   # a IA não leu nada sozinha


def test_rascunho_nao_vale_ate_confirmar(conn, com_scan):
    fid = com_scan["fid"]
    t = transcribe.transcribe(conn, com_scan["src"], com_scan["llm"], fid, "U-C")
    assert t["status"] == "rascunho" and "Carla confirmou a sala" in t["text"]
    assert com_scan["llm"].files[0][0] == "application/pdf"               # o arquivo foi anexado à IA
    run_sync(conn, com_scan["src"], llm=com_scan["llm"])
    assert _row(conn, fid)["sync_status"] == "nao_suportado" and not pending(conn)   # rascunho não entra


def test_confirmada_entra_e_gera_sugestao_com_trecho_conferido(conn, com_scan):
    fid = com_scan["fid"]
    transcribe.transcribe(conn, com_scan["src"], com_scan["llm"], fid, "U-C")
    transcribe.confirm(conn, fid, "U-B", LIDO)
    run_sync(conn, com_scan["src"], llm=com_scan["llm"])
    s = _row(conn, fid)
    assert s["sync_status"] == "ok" and s["role"] == "ata"
    p = pending(conn)
    assert len(p) == 1 and p[0]["target_activity_id"] == "ACT-103" and p[0]["proposed"]["status"] == "Em andamento"
    info = transcribe.confirmed_info(conn, fid)
    assert info["by"] == "U-B" and info["edited"] is False
    # o quadro só muda com revisão humana, como qualquer sugestão
    assert conn.execute("SELECT status FROM activities WHERE activity_id='ACT-103'").fetchone()[0] == "Bloqueada"


def test_correcao_da_pessoa_e_o_que_vale(conn, com_scan):
    fid = com_scan["fid"]
    transcribe.transcribe(conn, com_scan["src"], com_scan["llm"], fid, "U-C")
    corrigido = LIDO + "\nSala 12 do prédio AT7."
    transcribe.confirm(conn, fid, "U-C", corrigido)
    run_sync(conn, com_scan["src"], llm=com_scan["llm"])
    txt = conn.execute("SELECT extracted_text FROM source_versions sv JOIN sources s ON s.file_id=sv.file_id "
                       "AND s.content_hash=sv.content_hash WHERE s.file_id=?", (fid,)).fetchone()[0]
    assert "Sala 12 do prédio AT7." in txt and transcribe.confirmed_info(conn, fid)["edited"] is True


def test_arquivo_mudou_depois_volta_a_nao_processado(conn, com_scan, folder):
    fid = com_scan["fid"]
    transcribe.transcribe(conn, com_scan["src"], com_scan["llm"], fid, "U-C")
    transcribe.confirm(conn, fid, "U-C", LIDO)
    run_sync(conn, com_scan["src"], llm=com_scan["llm"])
    with open(folder / SCAN, "ab") as f:          # nova versão do arquivo no "Drive"
        f.write(b"\n%nova versao")
    run_sync(conn, com_scan["src"], llm=com_scan["llm"])
    s = _row(conn, fid)
    assert s["sync_status"] == "nao_suportado" and "transcreva de novo" in s["status_message"]


def test_sem_texto_e_falha_da_ia(conn, com_scan):
    fid = com_scan["fid"]
    vazio = FakeLLM(lambda s, u: {"text": "", "legivel": False, "observacao": None})
    t = transcribe.transcribe(conn, com_scan["src"], vazio, fid, "U-C")
    assert t["note"] == "A IA não encontrou texto legível neste arquivo."
    with pytest.raises(transcribe.TranscriptionError):
        transcribe.confirm(conn, fid, "U-C", "   ")

    class Caiu:
        model = "x"

        def complete_json(self, *a, **k):
            raise LLMError("HTTP 503")
    with pytest.raises(transcribe.TranscriptionError, match="não conseguiu transcrever"):
        transcribe.transcribe(conn, com_scan["src"], Caiu(), fid, "U-C")
    with pytest.raises(transcribe.TranscriptionError, match="desligada"):
        transcribe.transcribe(conn, com_scan["src"], None, fid, "U-C")


def test_formatos_que_nao_sao_imagem_nao_tem_botao():
    assert not transcribe.can_transcribe({"sync_status": "nao_suportado", "mime_type": "application/msword",
                                          "name": "ata.doc"})
    assert transcribe.can_transcribe({"sync_status": "nao_suportado", "mime_type": "image/png", "name": "q.png"})
    assert not transcribe.can_transcribe({"sync_status": "ok", "mime_type": "application/pdf", "name": "a.pdf"})


def test_pela_interface(client, monkeypatch):  # noqa: F811
    import central.app as app_module
    folder = os.environ["LOCAL_FOLDER"]
    shutil.copy(DATA / "04_EXTRAS" / SCAN, folder)
    client.post("/sincronizar")
    login(client, "U-C")
    fid = next(r for r in __import__("re").findall(r'href="/fontes/([^"]+)">' + SCAN, client.get("/fontes").text))
    pagina = client.get(f"/fontes/{fid}").text
    assert "PDF escaneado" in pagina and "Ler com ajuda da IA" in pagina
    monkeypatch.setattr(app_module, "make_vision_llm", lambda: FakeLLM(ia_que_le))
    monkeypatch.setattr(app_module, "make_llm", lambda: FakeLLM(ia_que_le))
    pagina = client.post(f"/fontes/{fid}/transcrever").text
    assert "Confira a transcrição" in pagina and "Carla confirmou a sala da oficina." in pagina
    assert client.get(f"/fontes/{fid}/original").headers["content-type"] == "image/png"
    pagina = client.post(f"/fontes/{fid}/transcricao/confirmar", data={"text": LIDO}).text
    assert "Transcrição confirmada" in pagina and "conferido por Carla" in pagina
    assert "transcrito pela IA" in client.get("/fontes").text
    assert "transcrito pela IA, conferido por Carla" in client.get("/sugestoes").text
