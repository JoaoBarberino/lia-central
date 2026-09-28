"""Cliente do Gemini: modelo reserva quando o principal está sobrecarregado."""
import json

import httpx
import pytest

from central import ai


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def test_usa_modelo_reserva_quando_principal_esta_sobrecarregado(monkeypatch):
    chamados = []

    def fake_post(url, **kw):
        chamados.append(url.split("/models/")[1].split(":")[0])
        if "principal" in url:
            return Resp(503, {"error": {"message": "high demand"}})
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"items": []}'}]}}],
                          "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    llm = ai.GeminiLLM("chave-falsa", "principal", "reserva")
    result, usage = llm.complete_json("s", "u")
    assert result == {"items": []} and llm.model == "reserva"
    assert chamados == ["principal"] * 3 + ["reserva"]


def test_erro_de_configuracao_nao_tenta_reserva(monkeypatch):
    chamados = []

    def fake_post(url, **kw):
        chamados.append(url)
        return Resp(400, {"error": {"message": "API key not valid"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(ai.LLMError, match="400"):
        ai.GeminiLLM("x", "principal", "reserva").complete_json("s", "u")
    assert len(chamados) == 1
