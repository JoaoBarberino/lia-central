"""Busca e filtros das listas (Minhas atividades, Todas as atividades, Documentos).

- A busca ignora acento e maiúsculas e exige todas as palavras ("sala oficina" acha "Sala da Oficina").
- Os filtros vêm da URL (?frente=Growth&prazo=vencidas): dá para salvar nos favoritos ou mandar o link.
- Tudo acontece no servidor, sobre os mesmos dados das telas: não há índice separado para ficar desatualizado.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta

from . import activities as acts
from .extractors import normalize

SEM = "__sem"   # valor de filtro para "Sem responsável" / "Sem frente"

PRAZOS = [("", "Qualquer prazo"), ("vencidas", "Vencidas"), ("perto", f"Vencem em até {acts.SOON_DAYS} dias"), ("mes", "Este mês"),
          ("sem", "Sem prazo")]
SITUACOES = [("", "Abertas"), ("A fazer", "A fazer"), ("Em andamento", "Em andamento"), ("Bloqueada", "Bloqueada"),
             ("Concluída", "Concluída"), ("todas", "Todas, com as concluídas")]
DOC_TIPOS = [("", "Todos os tipos"), ("atas", "Atas"), ("planilhas", "Planilhas"),
             ("referencia", "Guias e referência"), ("antigos", "Documentos antigos"), ("outros", "Outros arquivos")]
DOC_SITUACOES = [("", "Qualquer situação"), ("ok", "Lidos"), ("nao_suportado", "Não lidos"), ("erro", "Com erro"),
                 ("indisponivel", "Indisponíveis")]


def terms(q: str | None) -> list[str]:
    return [t for t in re.findall(r"\w+", normalize(q or "")) if t]


def matches(text: str, ts: list[str]) -> bool:
    flat = normalize(text or "")
    return all(t in flat for t in ts)


# ---------------------------------------------------------------------------
# Atividades
# ---------------------------------------------------------------------------
def filter_activities(items: list[dict], *, q: str = "", responsavel: str = "", frente: str = "", situacao: str = "",
                      prazo: str = "", novidade: bool = False, today: date, stale_days: int, names: dict) -> list[dict]:
    ts = terms(q)
    soon = (today + timedelta(days=acts.SOON_DAYS)).isoformat()
    month_end = (date(today.year + (today.month == 12), today.month % 12 + 1, 1) - timedelta(days=1)).isoformat()
    out = []
    for a in items:
        # situação: por padrão só as abertas
        if situacao == "todas":
            pass
        elif situacao:
            if a["status"] != situacao:
                continue
        elif a["status"] == "Concluída":
            continue
        if responsavel == SEM and a["owners"]:
            continue
        if responsavel and responsavel != SEM and responsavel not in a["owners"]:
            continue
        if frente == SEM and a.get("front"):
            continue
        if frente and frente != SEM and (a.get("front") or "") != frente:
            continue
        d = a.get("due_date")
        if prazo == "vencidas" and not (d and d < today.isoformat() and a["status"] != "Concluída"):
            continue
        if prazo == "perto" and not (d and today.isoformat() <= d <= soon and a["status"] != "Concluída"):
            continue
        if prazo == "mes" and not (d and today.isoformat()[:7] == d[:7] and d <= month_end):
            continue
        if prazo == "sem" and d:
            continue
        if novidade and not acts.days_without_news(a, today, stale_days):
            continue
        if ts:
            hay = " ".join(str(x or "") for x in (a["title"], a["activity_id"], a.get("next_step"), a.get("notes"),
                                                   a.get("description"), a.get("front"),
                                                   " ".join(names.get(o, o) for o in a["owners"])))
            if not matches(hay, ts):
                continue
        out.append(a)
    return out


def fronts(items: list[dict]) -> list[str]:
    return sorted({a["front"] for a in items if a.get("front")})


# ---------------------------------------------------------------------------
# Documentos (nome e conteúdo)
# ---------------------------------------------------------------------------
TIPO_ROLES = {"atas": {"ata"}, "planilhas": {"registro_oficial", "registro_candidato"},
              "referencia": {"indice", "estado_atual", "guia"}, "antigos": {"historico"}}


def _doc_text(v) -> str:
    if v is None:
        return ""
    if v["extracted_text"]:
        return v["extracted_text"]
    if v["extracted_json"]:
        cells = []
        for sh in json.loads(v["extracted_json"]).get("sheets", []):
            cells += [str(h) for h in sh.get("headers", [])]
            for r in sh.get("rows", []):
                cells += [str(c) for c in r.get("cells", {}).values() if c is not None]
        return " · ".join(cells)
    return ""


def snippet(text: str, ts: list[str], width: int = 90) -> dict | None:
    """Trecho em volta da primeira palavra encontrada: {'before','match','after'} (destaque sem HTML cru)."""
    if not ts or not text:
        return None
    # sem marcas de formatação (#, `, *, >) e numa linha só
    one_line = " ".join(re.sub(r"[`*>#]+|(?<!\w)_+|_+(?!\w)", " ", text).split())
    flat = normalize(one_line)   # normalize não muda o tamanho do texto em português (só tira acentos)
    if len(flat) != len(one_line):
        flat = one_line.lower()
    pos, term = min(((flat.find(t), t) for t in ts if flat.find(t) >= 0), default=(-1, ""))
    if pos < 0:
        return None
    start, end = max(0, pos - width), min(len(one_line), pos + len(term) + width)
    # não corta palavra ao meio
    if start > 0:
        sp = one_line.find(" ", start, pos)
        start = sp + 1 if sp >= 0 else start
    if end < len(one_line):
        sp = one_line.rfind(" ", pos + len(term), end)
        end = sp if sp >= 0 else end
    return {"before": ("…" if start else "") + one_line[start:pos], "match": one_line[pos:pos + len(term)],
            "after": one_line[pos + len(term):end] + ("…" if end < len(one_line) else "")}


def filter_sources(conn: sqlite3.Connection, rows: list[dict], *, q: str = "", tipo: str = "",
                   situacao: str = "") -> list[dict]:
    ts = terms(q)
    out = []
    for r in rows:
        if situacao and r["sync_status"] != situacao:
            continue
        if tipo:
            role = r.get("role") or ""
            if tipo == "outros":
                if any(role in roles for roles in TIPO_ROLES.values()):
                    continue
            elif role not in TIPO_ROLES.get(tipo, set()):
                continue
        r = dict(r, hit=None)
        if ts:
            in_name = matches(r["name"], ts)
            v = conn.execute("SELECT extracted_text, extracted_json FROM source_versions WHERE file_id=? AND content_hash=?",
                             (r["file_id"], r["content_hash"])).fetchone() if r.get("content_hash") else None
            text = _doc_text(v)
            in_text = matches(text, ts)
            if not (in_name or in_text):
                continue
            if in_text:
                r["hit"] = snippet(text, ts)
        out.append(r)
    return out
