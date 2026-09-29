"""Regra de autoridade: qual papel cada arquivo tem e o que ele pode mudar.

REGRA ÚNICA DE PRECEDÊNCIA (a mesma do INDEX.md do pacote):

  1. Decisão humana aprovada na aplicação  -> é o estado oficial da atividade.
  2. Proposta vinda de ata nova            -> vira SUGESTÃO pendente; nunca altera sozinha.
  3. Estado atual documentado              -> contexto (ESTADO-ATUAL.md), não altera tarefas.
  4. Registro inicial apontado pelo índice -> base da PRIMEIRA importação, identificado
                                               pelo file_id (não pelo nome).
  5. Arquivo antigo, homônimo ou sem autoridade confirmada -> nunca anula o registro;
                                               vira pendência visível quando parece concorrer.

Consequência prática: depois da importação, o banco da aplicação é a fonte oficial.
Tudo que chega do Drive depois (ata, planilha editada, planilha vazia) passa
por revisão humana. A data de modificação de um arquivo nunca decide nada sozinha.
"""
from __future__ import annotations

import re

from .extractors import Extracted, looks_like_register, normalize

ROLE_LABELS = {
    "indice": "Índice da pasta",
    "estado_atual": "Resumo da Liga",
    "guia": "Guia de entrada",
    "ata": "Ata de reunião",
    "registro_oficial": "Planilha oficial das atividades",
    "registro_candidato": "Planilha parecida com a oficial",
    "historico": "Documento antigo (substituído)",
    "outro": "Outro documento",
}

HISTORIC_STATUS = {"deprecated", "obsoleto", "substituido", "arquivado", "historico"}

# Ata não depende do nome do arquivo: "Reunião Growth 10-10" com título "Ata de reunião" também é ata.
_MINUTES_WORDS = re.compile(r"\b(ata|atas|minuta)\b")
_MEETING_WORDS = re.compile(r"\b(reuniao|encontro|alinhamento|retrospectiva|retro|assembleia)\b")
_DATE_IN_NAME = re.compile(r"\d{4}-\d{2}-\d{2}|\b\d{1,2}[-/]\d{1,2}\b")
_BODY_PEOPLE = re.compile(r"\b(participaram|presentes|estiveram presentes)\b")
_BODY_DECISION = re.compile(r"\b(decis|decidi|encaminhamento|proximos passos|ficou combinado|ficou decidido)")


def looks_like_minutes(name: str, ext: Extracted) -> bool:
    """Ata pelo cabeçalho, pelo nome ou título, ou pelo corpo (quem participou + o que foi decidido)."""
    if "data_da_reuniao" in ext.meta:
        return True
    words = normalize(re.sub(r"[_.]+", " ", name)) + " " + normalize(ext.title or "")
    if _MINUTES_WORDS.search(words):
        return True
    if _MEETING_WORDS.search(words) and _DATE_IN_NAME.search(words):   # "Reunião Growth 10-10", não "Como conduzir reuniões"
        return True
    head = normalize("\n".join((ext.text or "").splitlines()[:40]))
    return bool(_BODY_PEOPLE.search(head) and _BODY_DECISION.search(head))


def classify(name: str, ext: Extracted | None, is_official_register: bool) -> str:
    n = normalize(name)
    if ext is None:
        return "outro"
    if ext.kind == "table":
        if is_official_register:
            return "registro_oficial"
        if any(looks_like_register(s) for s in ext.sheets):
            return "registro_candidato"
        return "outro"
    status = normalize(ext.meta.get("status", ""))
    if status in HISTORIC_STATUS or ext.meta.get("substituido_por"):
        return "historico"
    if n.startswith("index"):
        return "indice"
    if n.startswith("estado-atual") or n.startswith("estado_atual"):
        return "estado_atual"
    if n.startswith("guia") or normalize(ext.title or "") == "comece aqui":
        return "guia"
    if looks_like_minutes(name, ext):
        return "ata"
    return "outro"


_POINTER = re.compile(r"`([^`]+\.xlsx)`(?:[^`\n]*?aba\s+`([^`]+)`)?", re.IGNORECASE)


def find_register_pointer(index_text: str) -> tuple[str, str | None] | None:
    """Procura no INDEX.md a linha que aponta a planilha-fonte das atividades.

    Exemplo no pacote: "- `Ata_registro.xlsx`, aba `Atividades`: **fonte inicial ...**"
    Só aceita a linha se ela falar em "fonte"; uma menção solta a uma planilha não basta.
    """
    for line in index_text.splitlines():
        m = _POINTER.search(line)
        if m and "fonte" in normalize(line):
            return m.group(1).strip(), (m.group(2).strip() if m.group(2) else None)
    return None
