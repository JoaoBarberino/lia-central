"""Correções da auditoria de requisitos (29/09)."""
from central.ai import FakeLLM, validate_item
from central.authority import classify
from central.extractors import parse_text_document

from .test_cenarios import pending


# --- B1: responsáveis ------------------------------------------------------------
def test_ia_que_adiciona_responsavel_nao_tira_os_atuais(conn, sync):
    sync()
    texto = "Bruno vai ajudar no fluxo de solicitação de materiais (ACT-104)."
    item = {"kind": "update", "target_activity_id": "ACT-104", "owners": ["U-A", "U-D", "U-B"],
            "evidence": texto, "uncertainties": []}
    clean, _ = validate_item(conn, item, texto)
    assert clean["proposed"]["owners"] == ["U-A", "U-B", "U-D"]      # Ana e Davi continuam


def test_remocao_de_responsavel_fica_explicita(conn, sync):
    sync()
    texto = "Davi assume sozinho o fluxo de materiais (ACT-104)."
    item = {"kind": "update", "target_activity_id": "ACT-104", "owners": ["U-D"], "evidence": texto,
            "uncertainties": []}
    clean, _ = validate_item(conn, item, texto)
    assert clean["proposed"]["owners"] == ["U-D"]
    assert any("tira Ana dos responsáveis" in u for u in clean["uncertainties"])


# --- B2: hipótese marcada como mudança ------------------------------------------------
def test_talvez_nunca_vira_sugestao(conn, sync, folder):
    sync()
    (folder / "Ata_2026-10-09.md").write_text(
        "# Ata de reunião de 9 de outubro de 2026\n\ndata_da_reuniao: 2026-10-09\n\n"
        "Talvez o checklist fique para 2026-10-20.\n", encoding="utf-8")

    def insistente(system, user):
        return {"items": [{"kind": "update", "target_activity_id": "ACT-102", "due_date": "2026-10-20",
                           "evidence": "Talvez o checklist fique para 2026-10-20.", "uncertainties": []}]}
    sync(llm=FakeLLM(insistente))
    assert pending(conn) == []
    nota = conn.execute("SELECT kind, reason FROM extraction_notes WHERE text LIKE 'Talvez%'").fetchone()
    assert nota["kind"] == "hipotese" and "possibilidade" in nota["reason"]


# --- B3: ata reconhecida sem depender do nome -----------------------------------------
def test_ata_reconhecida_pelo_titulo_ou_pelo_corpo():
    doc = parse_text_document("# Ata de reunião Growth\n\nParticiparam Ana e Bruno.\nAna fará X até 2026-10-12.")
    assert classify("Reunião Growth 10-10", doc, False) == "ata"
    corpo = parse_text_document("# Notas de 10/10\n\nParticiparam Ana e Davi.\n\nDecisões\n- Davi fará o kit.")
    assert classify("Notas 2026-10-10", corpo, False) == "ata"
    guia = parse_text_document("# Regras de Growth\n\nPosts passam pelo líder.")
    assert classify("Regras de Growth", guia, False) == "outro"


def test_ata_com_outro_nome_gera_sugestao(conn, sync, folder):
    sync()
    (folder / "Reunião Growth 10-10.md").write_text(
        "# Reunião Growth\n\nParticiparam Ana e Bruno.\n\nDecisões\n"
        "O prazo para entregar a versão de aprovação mudou de 2026-10-05 para 2026-10-07.\n", encoding="utf-8")
    sync()
    assert conn.execute("SELECT role FROM sources WHERE name='Reunião Growth 10-10.md'").fetchone()[0] == "ata"
    assert len(pending(conn)) == 1 and pending(conn)[0]["target_activity_id"] == "ACT-101"


# --- Lacunas ------------------------------------------------------------------------
from .test_web import client, login  # noqa: E402,F401  (fixture)


def test_menu_com_os_nomes_do_case(client):  # noqa: F811
    login(client, "U-A")
    home = client.get("/").text
    for nome in ("Comece aqui", "Minhas atividades", "Todas as atividades", "Sugestões para revisar",
                 "Novidades dos documentos", "Estado da sincronização"):
        assert nome in home, nome


def test_estado_da_sincronizacao_mostra_pasta_e_restricao(client):  # noqa: F811
    t = client.get("/sincronizacao").text
    assert "<h1>Estado da sincronização</h1>" in t and "Pasta conectada" in t and "só esta pasta e as subpastas" in t
    assert "Última atualização bem-sucedida" in t and "sem mudança" in t


def test_resumo_mostra_incerto_e_conflito(client):  # noqa: F811
    login(client, "U-C")
    t = client.get("/novidades?desde=30d").text
    assert "Em conflito, aguardando decisão" in t and "Planilha concorrente" in t   # a cópia vazia do pacote
    assert "Ainda não oficial" in t


def test_minhas_mostra_mudancas_recentes_e_atividade_mostra_ultima_atualizacao(client):  # noqa: F811
    login(client, "U-A")
    assert 'class="recent"' in client.get("/").text
    t = client.get("/atividades/ACT-101").text
    assert "Última atualização:" in t and "<dt>Descrição</dt>" in t


def test_revisor_completa_responsavel_que_faltava(client):  # noqa: F811
    import re
    login(client, "U-B")
    lista = client.get("/sugestoes").text
    sid = re.search(r'href="/sugestoes/(\d+)">Propor exercício', lista).group(1)
    pagina = client.get(f"/sugestoes/{sid}").text
    assert 'name="front"' in pagina                                   # campo que a ata não trouxe
    client.post(f"/sugestoes/{sid}/aceitar", data={"ajustar": "1", "owners": ["U-C"], "title": "Propor exercício prático da primeira oficina",
                                                   "due_date": "2026-10-10", "next_step": "Escolher um problema real simples",
                                                   "front": "Formação", "status": "A fazer"})
    assert "Formação" in client.get("/atividades/ACT-105").text


def test_video_nao_e_baixado(conn, sync, folder):
    sync()
    (folder / "gravacao.mp4").write_bytes(b"\x00" * 10)
    sync()
    r = conn.execute("SELECT * FROM sources WHERE name='gravacao.mp4'").fetchone()
    assert r["sync_status"] == "nao_suportado" and "Vídeo" in r["status_message"]


def test_nativo_do_google_nao_lido_vira_nao_processado():
    import pytest
    from central.extractors import Unsupported, skip_before_download
    with pytest.raises(Unsupported, match="formulário"):
        skip_before_download("application/vnd.google-apps.form", "Inscrições")
    skip_before_download("application/vnd.google-apps.document", "Ata")   # esse é lido


# --- Segunda rodada da auditoria --------------------------------------------------------
def test_decisao_com_talvez_no_meio_continua_sugestao(conn, sync):
    sync()
    texto = "Ficou decidido que Davi fará o checklist até 2026-10-12; talvez Carla ajude."
    item = {"kind": "update", "target_activity_id": "ACT-102", "due_date": "2026-10-12", "evidence": texto,
            "uncertainties": []}
    clean, _ = validate_item(conn, item, texto)
    assert clean["kind"] == "update" and clean["proposed"]["due_date"] == "2026-10-12"
    assert any("possibilidade" in u for u in clean["uncertainties"])
    texto2 = "Descartamos a hipótese de adiar; Ana entrega até 2026-10-07."
    clean2, _ = validate_item(conn, {"kind": "update", "target_activity_id": "ACT-101", "due_date": "2026-10-07",
                                     "evidence": texto2, "uncertainties": []}, texto2)
    assert clean2["kind"] == "update"


def test_guia_sobre_reunioes_nao_e_ata():
    assert classify("Como conduzir reuniões", parse_text_document("# Como conduzir reuniões\n\nDicas."), False) == "outro"
    equipe = parse_text_document("# Equipe\n\nParticipantes: Ana, Bruno.\nResponsável pela sede: Davi.")
    assert classify("Equipe", equipe, False) == "outro"


def test_nao_encontrado_responde_404(client):  # noqa: F811
    login(client, "U-A")
    assert client.get("/atividades/ACT-999").status_code == 404
    assert client.get("/sugestoes/999").status_code == 404
    assert client.post("/atividades/ACT-999/estado", data={"status": "Concluída"}).status_code == 404
