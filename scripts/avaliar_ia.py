"""Avalia a leitura de atas pela IA com atas novas e um gabarito.

As 18 atas de tests/dados/05_AVALIACAO_IA foram escritas por um agente independente, a partir só do enunciado
e da especificação (sem ver o prompt nem o código da IA), cada uma com o resultado esperado em gabarito.json.

Para cada ata, o script monta uma Central temporária com a carga inicial (as 4 atividades), coloca a ata na
pasta e roda a análise pelo mesmo caminho do site (IA + checagem em código). Depois compara as sugestões e o
que ficou de fora com o gabarito. Nada é gravado no banco de verdade.

Uso (com GEMINI_API_KEY no .env):
    python scripts/avaliar_ia.py              # todas as atas
    python scripts/avaliar_ia.py 09 15        # só alguns casos
    python scripts/avaliar_ia.py --simulado   # sem IA de verdade: só confere que o script funciona

O resultado aparece no terminal e fica salvo em data/avaliacao_ia.md.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from central import activities as acts  # noqa: E402
from central.ai import FakeLLM, LLMError, analyze_minutes  # noqa: E402
from central.config import get_settings  # noqa: E402
from central.db import connect, init_db  # noqa: E402
from central.extractors import normalize  # noqa: E402
from central.sources import LocalSource  # noqa: E402
from central.sync import run_sync  # noqa: E402

DADOS = RAIZ / "tests" / "dados"
AVAL = DADOS / "05_AVALIACAO_IA"


# ---------------------------------------------------------------------------
# Rodar uma ata
# ---------------------------------------------------------------------------
def montar_base(tmp: Path) -> tuple[Path, Path]:
    """Carga inicial feita uma vez (sem IA); cada caso começa de uma cópia dela."""
    pasta = tmp / "base_pasta"
    shutil.copytree(DADOS / "01_CARGA_INICIAL", pasta)
    banco = tmp / "base.db"
    conn = connect(banco)
    init_db(conn)
    run_sync(conn, LocalSource(pasta), llm=None)
    conn.close()
    return pasta, banco


def rodar_caso(tmp: Path, base_pasta: Path, base_banco: Path, ata: Path, llm) -> dict:
    pasta = tmp / f"caso_{ata.stem}"
    shutil.copytree(base_pasta, pasta)
    banco = tmp / f"caso_{ata.stem}.db"
    shutil.copy(base_banco, banco)
    shutil.copy(ata, pasta / ata.name)
    conn = connect(banco)
    init_db(conn)
    run_sync(conn, LocalSource(pasta), llm=None)          # lê a ata (sem analisar)
    fid = conn.execute("SELECT file_id FROM sources WHERE name=?", (ata.name,)).fetchone()["file_id"]
    for tentativa in range(3):                             # 503 da IA: espera e tenta de novo
        try:
            msg = analyze_minutes(conn, llm, fid)
            break
        except LLMError as e:
            if tentativa == 2:
                conn.close()
                return {"erro_ia": str(e)}
            time.sleep(15)
    nomes = {r["member_id"]: r["display_name"] for r in conn.execute("SELECT member_id, display_name FROM members")}
    sugestoes = []
    for r in conn.execute("SELECT * FROM suggestions WHERE source_file_id=? ORDER BY suggestion_id", (fid,)):
        prop = json.loads(r["proposed_fields"])
        if "owners" in prop:
            prop["owners"] = [nomes.get(o, o) for o in prop["owners"] or []]
        sugestoes.append({"tipo": r["kind"], "alvo": r["target_activity_id"], "campos": prop,
                          "incertezas": json.loads(r["uncertainties"] or "[]"), "evidencia": r["evidence"]})
    notas = [{"tipo": n["kind"], "texto": (n["text"] or "")[:160], "motivo": n["reason"]}
             for n in conn.execute("SELECT * FROM extraction_notes WHERE file_id=?", (fid,))]
    conn.close()
    return {"mensagem": msg, "sugestoes": sugestoes, "notas": notas}


# ---------------------------------------------------------------------------
# Comparar com o gabarito
# ---------------------------------------------------------------------------
def _vazio(v) -> bool:
    return v in (None, "", [])


def _bate_campo(campo: str, esperado, obtido) -> bool:
    if esperado is None:
        return _vazio(obtido)
    if campo == "owners":
        return sorted(normalize(x) for x in esperado) == sorted(normalize(x) for x in (obtido or []))
    return (obtido or None) == esperado


def _mesma(e: dict, s: dict) -> bool:
    if e["tipo"] != s["tipo"]:
        return False
    if e["tipo"] == "update":
        return s["alvo"] == e.get("alvo")
    titulo = normalize(s["campos"].get("title") or "")
    return all(normalize(p) in titulo for p in e.get("titulo_contem", []))


def _viola(regra: dict, s: dict) -> bool:
    if "tipo" in regra and "campo" not in regra:
        return s["tipo"] == regra["tipo"] and (regra["tipo"] != "update" or s["alvo"] == regra.get("alvo"))
    v = s["campos"].get(regra["campo"])
    if "contem" in regra:
        return normalize(regra["contem"]) in [normalize(x) for x in (v or [])]
    return v == regra.get("valor")


def avaliar(caso: dict, r: dict) -> tuple[str, list[str]]:
    if "erro_ia" in r:
        return "IA indisponível", [r["erro_ia"]]
    problemas = []
    sugs = r["sugestoes"]
    usadas = set()
    if caso["nenhuma_sugestao"] and sugs:
        problemas.append(f"esperava nenhuma sugestão, veio {len(sugs)}")
    for e in caso["esperado"]:
        achou = next((i for i, s in enumerate(sugs) if i not in usadas and _mesma(e, s)), None)
        rotulo = e.get("alvo") or "nova (" + ", ".join(e.get("titulo_contem", [])) + ")"
        if achou is None:
            problemas.append(f"faltou a sugestão {e['tipo']} {rotulo}")
            continue
        usadas.add(achou)
        s = sugs[achou]
        for campo, valor in (e.get("campos") or {}).items():
            if not _bate_campo(campo, valor, s["campos"].get(campo)):
                problemas.append(f"{rotulo}: {campo} esperado {valor!r}, veio {s['campos'].get(campo)!r}")
        if e.get("incerteza") and not s["incertezas"]:
            problemas.append(f"{rotulo}: esperava um ponto para conferir, não veio")
    for regra in caso.get("proibido", []):
        for s in sugs:
            if _viola(regra, s):
                problemas.append(f"proibido: {json.dumps(regra, ensure_ascii=False)}")
                break
    extras = [s for i, s in enumerate(sugs) if i not in usadas]
    if extras and not caso["nenhuma_sugestao"]:
        problemas.append(f"{len(extras)} sugestão(ões) a mais: "
                         + "; ".join(f"{s['tipo']} {s['alvo'] or s['campos'].get('title')}" for s in extras))
    return ("passou" if not problemas else "falhou"), problemas


def resumo_obtido(r: dict) -> str:
    if "erro_ia" in r:
        return "—"
    partes = []
    for s in r["sugestoes"]:
        campos = ", ".join(f"{k}={v}" for k, v in s["campos"].items() if k != "title")
        titulo = f" \"{s['campos'].get('title')}\"" if s["tipo"] == "create" else ""
        inc = f" [conferir: {'; '.join(s['incertezas'])}]" if s["incertezas"] else ""
        partes.append(f"{s['tipo']} {s['alvo'] or ''}{titulo}: {campos}{inc}")
    for n in r["notas"]:
        partes.append(f"deixou de fora ({n['tipo']}): {n['motivo'] or n['texto']}")
    return " | ".join(partes) or "nada"


# ---------------------------------------------------------------------------
def main(args: list[str]) -> int:
    simulado = "--simulado" in args
    filtro = [a for a in args if not a.startswith("--")]
    settings = get_settings()
    if simulado:
        llm = FakeLLM(lambda system, user: {"items": []})
    elif settings.llm_enabled:
        from central.ai import GeminiLLM
        llm = GeminiLLM(settings.gemini_api_key, settings.gemini_model, settings.gemini_fallback_model)
    else:
        print("GEMINI_API_KEY não configurada no .env. Use --simulado só para testar o script.")
        return 1

    gabarito = json.loads((AVAL / "gabarito.json").read_text(encoding="utf-8"))["casos"]
    casos = [c for c in gabarito if not filtro or c["id"] in filtro]
    linhas = [f"# Avaliação da IA ({'simulada' if simulado else settings.gemini_model})", "",
              "| # | Caso | Resultado | O que faltou ou sobrou | O que a Central fez |", "|---|---|---|---|---|"]
    placar = {"passou": 0, "falhou": 0, "IA indisponível": 0}
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        base_pasta, base_banco = montar_base(tmp)
        for caso in casos:
            inicio = time.time()
            r = rodar_caso(tmp, base_pasta, base_banco, AVAL / "atas" / caso["arquivo"], llm)
            veredito, problemas = avaliar(caso, r)
            placar[veredito] += 1
            marca = {"passou": "OK ", "falhou": "XX ", "IA indisponível": "?? "}[veredito]
            print(f"{marca} {caso['id']} {caso['categoria']} ({time.time() - inicio:.0f}s)")
            for p in problemas:
                print(f"      - {p}")
            obtido = resumo_obtido(r).replace("|", "/")
            linhas.append(f"| {caso['id']} | {caso['categoria']} | {veredito} | {'; '.join(problemas) or '—'} | {obtido} |")
    total = len(casos)
    fim = f"Passou em {placar['passou']} de {total}" + (
        f" ({placar['IA indisponível']} sem resposta da IA)" if placar["IA indisponível"] else "")
    print("\n" + fim)
    linhas += ["", f"**{fim}.**"]
    saida = RAIZ / "data" / "avaliacao_ia.md"
    saida.parent.mkdir(exist_ok=True)
    saida.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"Relatório salvo em {saida.relative_to(RAIZ)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
