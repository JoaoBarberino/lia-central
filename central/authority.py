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
    "indice": "Índice do acervo",
    "estado_atual": "Estado atual",
    "guia": "Guia de entrada",
    "ata": "Ata de reunião",
    "registro_oficial": "Registro oficial de atividades",
    "registro_candidato": "Planilha com formato de registro (sem autoridade)",
    "historico": "Histórico (substituído)",
    "outro": "Outro documento",
}

HISTORIC_STATUS = {"deprecated", "obsoleto", "substituido", "arquivado", "historico"}


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
    if "data_da_reuniao" in ext.meta or n.startswith("ata"):
        return "ata"
    if n.startswith("guia") or normalize(ext.title or "") == "comece aqui":
        return "guia"
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
