"""Sugestões legíveis: antes → depois, o que já está no quadro, trecho explicado."""
import sqlite3

from central import suggestions as sugg
from central import views
from central.ai import FakeLLM
from central.db import init_db

from .test_cenarios import add_file, pending
from .test_web import client, login  # noqa: F401  (fixture)


def _ia(system, user):
    if "mudou de 2026-10-05 para" in user:
        return {"items": [{"kind": "update", "target_activity_id": "ACT-101", "due_date": "2026-10-07",
                           "notes": "Bruno aprova a versão final antes de publicar",
                           "evidence": "O prazo para entregar a versão de aprovação mudou de 2026-10-05 para 2026-10-07.",
                           "reason": "Decisão registrada na ata", "uncertainties": []}]}
    return {"items": []}


def test_o_que_ja_esta_no_quadro_fica_registrado(conn, sync, folder):
    sync()
    conn.execute("UPDATE activities SET due_date='2026-10-07' WHERE activity_id='ACT-101'")
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-03.md", as_gdoc=True)
    sync(llm=FakeLLM(_ia))
    s = pending(conn)[0]
    assert list(s["proposed"]) == ["notes"] and s["already"] == {"due_date": "2026-10-07"}
    names = views.member_names(conn)
    assert views.sug_already(s, names) == ["Prazo 07/10/2026"]
    s["source"] = views.source_link(conn, s["source_file_id"])
    assert views.sug_origin(s) == "pela ata de 03/10"


def test_antes_e_depois(conn, sync, folder):
    sync()
    add_file(folder, "02_ADICIONAR_DEPOIS_DA_CARGA", "Ata_2026-10-03.md", as_gdoc=True)
    sync()
    s = pending(conn)[0]
    rows = {r["field"]: r for r in views.sug_rows(s, views.member_names(conn))}
    assert rows["due_date"]["old"] == "05/10/2026" and rows["due_date"]["new"] == "07/10/2026"
    assert rows["due_date"]["short"] and not rows["next_step"]["short"]


def test_banco_antigo_ganha_a_coluna_nova(tmp_path):
    c = sqlite3.connect(tmp_path / "velho.db")
    c.execute("CREATE TABLE suggestions (suggestion_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target_activity_id TEXT, "
              "proposed_fields TEXT NOT NULL, current_fields TEXT, evidence TEXT NOT NULL, reason TEXT, doc_date TEXT, "
              "uncertainties TEXT, source_file_id TEXT NOT NULL, source_version TEXT NOT NULL, review_status TEXT NOT NULL, "
              "reviewer_id TEXT, reviewed_at TEXT, review_note TEXT, model TEXT, created_at TEXT NOT NULL, "
              "dedupe_key TEXT NOT NULL UNIQUE)")
    c.row_factory = sqlite3.Row
    init_db(c)
    assert "already_fields" in {r[1] for r in c.execute("PRAGMA table_info(suggestions)")}


def test_telas(client):  # noqa: F811
    login(client, "U-B")
    lista = client.get("/sugestoes").text
    assert "Mudança sugerida pela ata de 03/10" in lista and "Atividade nova sugerida pela ata de 04/10" in lista
    assert "05/10/2026</span>" in lista and "07/10/2026</ins>" in lista and "A ata diz:" in lista
    import re
    sid = re.search(r'href="/sugestoes/(\d+)">Preparar carrossel', lista).group(1)
    pagina = client.get(f"/sugestoes/{sid}").text
    assert "O que muda ao aceitar" in pagina and "De onde veio" in pagina and "Trecho da ata de 03/10/2026" in pagina
    assert "Explicação da IA:" in pagina and "Detalhes técnicos" in pagina
