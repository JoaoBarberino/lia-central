"""Usabilidade e acessibilidade (§8): teclado, contraste de campos, bloquear/reabrir, sugestão pendente discreta,
tela de sugestões para quem não aprova."""
import os
import re
from pathlib import Path

from central import activities as acts
from central.db import connect

from .test_web import client, login  # noqa: F401  (fixture)

CSS = (Path(__file__).parent.parent / "central" / "static" / "style.css").read_text(encoding="utf-8")
TEMPLATES = Path(__file__).parent.parent / "central" / "templates"


def _db():
    return connect(os.environ["DATABASE_PATH"])


def _contraste(a: str, b: str) -> float:
    def lum(h):
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ---------------------------------------------------------------------------
# Teclado: nada é enviado sozinho ao mudar uma opção (WCAG 3.2.2)
# ---------------------------------------------------------------------------
def test_nenhuma_selecao_envia_sozinha(client):  # noqa: F811
    for t in TEMPLATES.glob("*.html"):
        assert "onchange" not in t.read_text(encoding="utf-8"), t.name
    login(client, "U-A")
    for url in ("/atividades", "/fontes"):
        pagina = client.get(url).text
        assert "Aplicar filtros" in pagina and 'id="resultados"' in pagina
    assert pagina.count(">Trocar</button>") == 2     # troca de pessoa: no topo e no menu do celular


def test_borda_dos_campos_tem_contraste(client):  # noqa: F811
    cor = re.search(r"--field:\s*(#[0-9A-Fa-f]{6})", CSS).group(1)
    assert _contraste(cor, "#FFFFFF") >= 3            # WCAG 1.4.11: contorno de campo
    assert "border: 1px solid #8C95B5" not in CSS


# ---------------------------------------------------------------------------
# Bloquear exige motivo e não apaga as notas; concluída pode ser reaberta
# ---------------------------------------------------------------------------
def test_bloquear_exige_motivo(client):  # noqa: F811
    login(client, "U-A")
    r = client.post("/atividades/ACT-101/estado", data={"status": "Bloqueada", "reason": "  "}, follow_redirects=False)
    assert "bloquear=1" in r.headers["location"]
    assert acts.snapshot(_db(), "ACT-101")["status"] == "Em andamento"
    pagina = client.get(r.headers["location"]).text
    assert "Escreva o motivo" in pagina and 'aria-invalid="true"' in pagina


def test_bloquear_guarda_motivo_sem_apagar_notas(client):  # noqa: F811
    login(client, "U-A")
    notas = acts.snapshot(_db(), "ACT-101")["notes"]
    assert notas
    client.post("/atividades/ACT-101/estado", data={"status": "Bloqueada", "reason": "Sala sem projetor"})
    a = acts.snapshot(_db(), "ACT-101")
    assert a["status"] == "Bloqueada" and a["notes"] == notas
    assert "Bloqueio: Sala sem projetor" in client.get("/").text
    pagina = client.get("/atividades/ACT-101").text
    assert "Motivo do bloqueio" in pagina and "Sala sem projetor" in pagina and notas in pagina


def test_bloqueio_da_planilha_continua_mostrando_as_notas(client):  # noqa: F811
    c = _db()
    blocked = [a for a in acts.list_activities(c, include_done=True) if a["status"] == "Bloqueada"]
    assert blocked                                           # a planilha de exemplo tem atividade bloqueada
    for a in blocked:
        assert a["block_reason"] == a["notes"]


def test_reabrir_volta_para_a_situacao_anterior(client):  # noqa: F811
    login(client, "U-A")
    client.post("/atividades/ACT-101/estado", data={"status": "Concluída"})
    pagina = client.get("/atividades/ACT-101").text
    assert "Reabrir" in pagina and "Marcar como bloqueada" not in pagina
    client.post("/atividades/ACT-101/estado", data={"status": "Em andamento"})
    assert acts.snapshot(_db(), "ACT-101")["status"] == "Em andamento"
    assert acts.activity_history(_db(), "ACT-101")[0]["reason"] == "Reaberta (voltou para Em andamento)"


# ---------------------------------------------------------------------------
# Sugestão pendente: o valor oficial vem primeiro; o sugerido fica discreto
# ---------------------------------------------------------------------------
def test_prazo_sugerido_nao_parece_oficial(client):  # noqa: F811
    login(client, "U-A")
    pagina = client.get("/atividades/ACT-101").text
    faixa = pagina[pagina.index('id="sugestoes"'):pagina.index("</section>", pagina.index('id="sugestoes"'))]
    assert "<ins>" not in faixa and "valem os valores oficiais" in faixa
    prazo = pagina[pagina.index("<dt>Prazo</dt>"):]
    oficial, sugerido = prazo.index("05/10/2026"), prazo.index("Sugerido, aguardando revisão")
    assert oficial < sugerido


# ---------------------------------------------------------------------------
# Quem não aprova acompanha as sugestões, sem botão que leva a um beco sem saída
# ---------------------------------------------------------------------------
def test_sugestoes_para_quem_nao_aprova(client):  # noqa: F811
    login(client, "U-A")                                   # Ana não aprova
    lista = client.get("/sugestoes").text
    assert ">Revisar</a>" not in lista and "Ver detalhes" in lista and "Quem aceita ou rejeita" in lista
    assert 'pendentes</span>' not in lista                 # sem contador no menu
    assert "troque a pessoa" not in client.get("/sugestoes/1").text
    login(client, "U-B")                                   # Bruno aprova
    lista = client.get("/sugestoes").text
    assert ">Revisar</a>" in lista and 'pendentes</span>' in lista
