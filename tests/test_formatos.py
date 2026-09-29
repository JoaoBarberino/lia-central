"""Formatos lidos sem IA: .docx, .pptx, .csv e .txt. Imagens e scans continuam "não processados", com motivo."""
import io
import shutil
import zipfile

from central.extractors import ExtractionError, Unsupported, extract, parse_csv, parse_pptx
from central.sources import DOCX

from .conftest import DATA
from .test_cenarios import open_issues, pending


def _pptx(slides: list[list[str]]) -> bytes:
    """Monta um .pptx mínimo (só o que o leitor usa)."""
    ns = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, paras in enumerate(slides, 1):
            body = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paras)
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="x" {ns}><p:txBody>{body}</p:txBody></p:sld>')
    return buf.getvalue()


def test_docx_do_pacote_vira_ata_com_sugestao(conn, sync, folder):
    sync()
    shutil.copy(DATA / "02_ADICIONAR_DEPOIS_DA_CARGA" / "Ata_2026-10-03.docx", folder)
    sync()
    src = conn.execute("SELECT * FROM sources WHERE name='Ata_2026-10-03.docx'").fetchone()
    assert src["sync_status"] == "ok" and src["role"] == "ata"
    p = pending(conn)
    assert len(p) == 1 and p[0]["target_activity_id"] == "ACT-101" and p[0]["proposed"]["due_date"] == "2026-10-07"


def test_docx_tabela_e_quebras():
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    xml = (f'<w:document {w}><w:body>'
           '<w:p><w:r><w:t>Ata de reunião</w:t></w:r></w:p>'
           '<w:p><w:r><w:t>Primeira linha</w:t><w:br/><w:t>segunda linha</w:t></w:r></w:p>'
           '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Tarefa</w:t></w:r></w:p></w:tc>'
           '<w:tc><w:p><w:r><w:t>Prazo</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
           '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    e = extract(DOCX, "ata.docx", buf.getvalue())
    assert e.title == "Ata de reunião" and "Primeira linha\nsegunda linha" in e.text and "Tarefa | Prazo" in e.text


def test_pptx_texto_por_slide(conn, sync, folder):
    e = parse_pptx(_pptx([["Planejamento do semestre", "Oficina em 17/10"], ["Responsável: Carla"]]))
    assert e.title == "Slide 1" and "Oficina em 17/10" in e.text and "Slide 2\nResponsável: Carla" in e.text
    sync()
    (folder / "Apresentacao_kickoff.pptx").write_bytes(_pptx([["Kickoff da Liga", "Frentes: Growth e Formação"]]))
    sync()
    assert conn.execute("SELECT sync_status FROM sources WHERE name='Apresentacao_kickoff.pptx'").fetchone()[0] == "ok"


def test_csv_com_colunas_de_registro_nao_substitui_o_oficial(conn, sync, folder):
    sync()
    (folder / "atividades_exportadas.csv").write_text(
        "ID;Atividade;Responsáveis;Prazo\nACT-101;Preparar carrossel;Ana;2026-12-31\n", encoding="utf-8")
    sync()
    src = conn.execute("SELECT * FROM sources WHERE name='atividades_exportadas.csv'").fetchone()
    assert src["sync_status"] == "ok" and src["role"] == "registro_candidato"
    assert open_issues(conn, "registro_homonimo")        # vira pendência para uma pessoa decidir
    a = conn.execute("SELECT due_date FROM activities WHERE activity_id='ACT-101'").fetchone()
    assert a["due_date"] == "2026-10-05"                  # o quadro não mudou


def test_csv_separador_e_acentos():
    e = parse_csv("Nome,Frente\nAna,Growth\nCarla,Formação\n".encode("cp1252"), "membros.csv")
    assert e.sheets[0]["headers"] == ["Nome", "Frente"] and e.sheets[0]["rows"][1]["cells"]["Frente"] == "Formação"


def test_imagem_e_scan_continuam_nao_processados(conn, sync, folder):
    sync()
    shutil.copy(DATA / "04_EXTRAS" / "foto_quadro.png", folder)
    shutil.copy(DATA / "04_EXTRAS" / "Ata_2026-10-08_digitalizada.pdf", folder)
    sync()
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM sources WHERE sync_status='nao_suportado'")}
    assert "texto dentro de imagens" in rows["foto_quadro.png"]["status_message"]
    assert "PDF escaneado" in rows["Ata_2026-10-08_digitalizada.pdf"]["status_message"]
    assert not pending(conn)                              # nada inventado


def test_arquivo_corrompido_vira_erro_visivel(conn, sync, folder):
    sync()
    (folder / "Ata_quebrada.docx").write_bytes(b"isto nao e um zip")
    sync()
    r = conn.execute("SELECT * FROM sources WHERE name='Ata_quebrada.docx'").fetchone()
    assert r["sync_status"] == "erro" and "Não foi possível abrir" in r["status_message"]
    assert open_issues(conn, "erro_leitura")


def test_formato_antigo_diz_como_resolver():
    try:
        extract("application/msword", "ata.doc", b"...")
    except Unsupported as u:
        assert "Salve como .docx" in str(u)
    else:
        raise AssertionError("deveria ser Unsupported")
    try:
        extract(DOCX, "vazio.docx", b"PK")
    except ExtractionError:
        pass
