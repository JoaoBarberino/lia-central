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


# ===========================================================================
# Pacote 2: Comece aqui, painel de ajuste, "perto do prazo", pendências, erros
# ===========================================================================
def test_comece_aqui_primeira_acao_e_documentos(client):  # noqa: F811
    login(client, "U-A")
    pagina = client.get("/comece-aqui").text
    acao = pagina[pagina.index('class="first-action"'):pagina.index("</section>", pagina.index('class="first-action"'))]
    assert "Abrir a atividade" in acao and 'href="/atividades/ACT-101"' in acao and "prazo mais próximo" in acao
    # passos do guia: o texto é do documento, mas os nomes de arquivo viram links
    passos = pagina[pagina.index('class="steps"'):]
    assert re.search(r'<a href="/fontes/[^"]+">Ata_registro\.xlsx</a>', passos)
    assert "O que o guia de entrada recomenda" in pagina
    # tabela de referência: título legível primeiro, nome do arquivo ao lado
    assert re.search(r'<a href="/fontes/[^"]+">Resumo da Liga</a>', pagina)


def test_links_de_documento_escapam_o_texto():
    from central.app import _com_links
    html = str(_com_links("Leia <b>INDEX.md</b> e INDEX.md.bak", {"INDEX.md": "f1"}))
    assert "&lt;b&gt;" in html and html.count('<a href="/fontes/f1">INDEX.md</a>') == 2


def test_aviso_de_primeira_visita(client):  # noqa: F811
    login(client, "U-D")
    assert "Primeira vez aqui?" in client.get("/").text
    client.get("/comece-aqui")
    assert "Primeira vez aqui?" not in client.get("/").text
    login(client, "U-A")
    client.post("/comece-aqui/depois")
    assert "Primeira vez aqui?" not in client.get("/").text


def test_perto_do_prazo_tem_um_criterio_so(client):  # noqa: F811
    assert acts.SOON_DAYS == 3
    for t in ("minhas.html", "novidades.html"):
        texto = (TEMPLATES / t).read_text(encoding="utf-8")
        assert "3 dias" not in texto and "7dias" not in texto          # o número vem de SOON_DAYS
    login(client, "U-A")
    home = client.get("/").text
    assert "Vencem em até 3 dias" in home                               # atalho igual ao resumo e ao selo


def test_ajuste_mostra_um_botao_e_mensagem_certa_ao_criar(client):  # noqa: F811
    pagina = (TEMPLATES / "sugestao.html").read_text(encoding="utf-8")
    assert "accept-plain" in pagina and "Cancelar ajuste" in pagina
    assert ".review-panel:has(details.adjust[open]) .accept-plain { display: none; }" in CSS
    login(client, "U-B")
    lista = client.get("/sugestoes").text
    sid = re.search(r'href="/sugestoes/(\d+)">Propor exercício prático', lista).group(1)
    r = client.post(f"/sugestoes/{sid}/aceitar", data={}, follow_redirects=True)
    assert "entrou no quadro como atividade nova" in r.text and "foi atualizada" not in r.text


def test_documento_leva_a_pendencia(client):  # noqa: F811
    login(client, "U-A")
    c = _db()
    issue = c.execute("SELECT issue_id, file_id FROM issues WHERE status='aberta' AND file_id IS NOT NULL").fetchone()
    assert issue
    assert f'href="/pendencias#pendencia-{issue["issue_id"]}"' in client.get("/fontes").text
    assert "Este documento tem uma pendência aberta" in client.get(f"/fontes/{issue['file_id']}").text
    assert f'id="pendencia-{issue["issue_id"]}"' in client.get("/pendencias").text


def test_mensagens_para_leitor_de_tela(client):  # noqa: F811
    login(client, "U-A")
    r = client.post("/atividades/ACT-101/estado", data={"status": "Concluída"})
    assert 'class="flash ok" role="status"' in r.text
    r = client.post("/atividades/nova", data={"title": ""})
    assert 'role="alert"' in r.text or 'aria-invalid="true"' in r.text
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "querySelector('[aria-invalid=\"true\"]')" in base          # foco no primeiro campo com erro


# ===========================================================================
# Correções dos prints e Pacote 3
# ===========================================================================
def _sid(client, titulo):  # noqa: F811
    return re.search(r'href="/sugestoes/(\d+)">' + re.escape(titulo), client.get("/sugestoes").text).group(1)


def test_ajuste_preserva_o_que_a_ata_sugeriu(client):  # noqa: F811
    login(client, "U-B")
    sid = _sid(client, "Propor exercício prático")
    client.post(f"/sugestoes/{sid}/aceitar", data={"ajustar": "1", "title": "Propor exercício prático da primeira oficina",
                                                   "owners": ["U-C"], "due_date": "2026-10-12",
                                                   "next_step": "Escolher um problema real simples"})
    from central import suggestions as sugg
    s = sugg.get(_db(), int(sid))
    assert s["review_status"] == "aceita_com_ajuste"
    assert s["proposed"]["due_date"] == "2026-10-10"             # a proposta original continua guardada
    assert s["applied"]["due_date"] == "2026-10-12" and s["adjusted"] == ["due_date"]
    pagina = client.get(f"/sugestoes/{sid}").text
    assert "Sugestão aceita com ajuste" in pagina and "A ata sugeriu: 10/10/2026" in pagina
    assert re.search(r'class="btn secondary" href="/atividades/ACT-\d+">Ver a atividade', pagina)  # há outra pendente
    client.post(f"/sugestoes/{_sid(client, 'Preparar carrossel')}/rejeitar", data={"reason": "teste"})
    pagina = client.get(f"/sugestoes/{sid}").text
    assert re.search(r'class="btn" href="/atividades/ACT-\d+">Ver a atividade', pagina)   # sem próxima: é a principal


def test_campos_longos_em_varias_linhas(client):  # noqa: F811
    login(client, "U-B")
    pagina = client.get(f"/sugestoes/{_sid(client, 'Preparar carrossel')}").text
    assert re.search(r'<textarea class="grow"[^>]*name="next_step"', pagina)
    assert 'textarea class="grow" rows="1" autocomplete="off" id="next_step"' in client.get("/atividades/ACT-101/editar").text


def test_atividade_criada_por_sugestao_mostra_origem_e_o_que_falta(client):  # noqa: F811
    login(client, "U-B")
    sid = _sid(client, "Propor exercício prático")
    client.post(f"/sugestoes/{sid}/aceitar", data={})
    act = sugg_target = __import__("central.suggestions", fromlist=["get"]).get(_db(), int(sid))["target_activity_id"]
    pagina = client.get(f"/atividades/{act}", headers={"referer": f"http://testserver/sugestoes/{sid}"}).text
    assert f"aprovada por Bruno" in pagina and f'href="/sugestoes/{sid}"' in pagina
    assert "Falta: frente." in pagina and f'href="/atividades/{act}/editar">Completar' in pagina
    assert f'<a class="back" href="/sugestoes/{sid}">Sugestão #{sid}</a>' in pagina
    assert sugg_target


def test_voltar_mantem_a_lista_de_origem(client):  # noqa: F811
    login(client, "U-A")
    pagina = client.get("/atividades/ACT-101", headers={"referer": "http://testserver/atividades?prazo=vencidas"}).text
    assert '<a class="back" href="/atividades?prazo=vencidas">Todas as atividades</a>' in pagina
    # depois de mudar a situação a página volta para si mesma, mas o "voltar" continua o mesmo
    pagina = client.get("/atividades/ACT-101", headers={"referer": "http://testserver/atividades/ACT-101"}).text
    assert '<a class="back" href="/atividades?prazo=vencidas">' in pagina


def test_titulo_da_aba_e_data_de_concluida(client):  # noqa: F811
    login(client, "U-A")
    assert "<title>Preparar carrossel sobre ferramentas | Central da Liga</title>" in client.get("/atividades/ACT-101").text
    client.post("/atividades/ACT-101/estado", data={"status": "Concluída"})
    assert 'class="date done"' in client.get("/atividades/ACT-101").text


def test_ordenar_mantem_filtros(client):  # noqa: F811
    login(client, "U-D")
    pagina = client.get("/?q=checklist&prazo=perto").text
    assert 'href="/?q=checklist&amp;prazo=perto&amp;ordem=estado"' in pagina
    assert 'href="/?q=checklist#resultados" aria-current="true">Vencem' in pagina         # clicar de novo tira o atalho


def test_busca_de_documentos_ignora_cabecalho_tecnico(client):  # noqa: F811
    login(client, "U-A")
    pagina = client.get("/fontes?q=ativo").text
    assert "status: ativo" not in pagina and "data_da_reuniao" not in pagina


def test_atualizar_agora_so_para_quem_entrou(client):  # noqa: F811
    assert "Atualizar agora" not in client.get("/entrar").text
    assert client.post("/sincronizar", follow_redirects=False).headers["location"] == "/entrar"
    login(client, "U-A")
    assert "Atualizar agora" in client.get("/").text
    assert "tokens" not in client.get("/sincronizacao").text.split("Detalhes técnicos")[0]
