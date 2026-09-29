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
