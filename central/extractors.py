"""Extratores por formato: transformam bytes em texto/tabelas + metadados.

Cada formato tem seu extrator. Formatos fora do escopo retornam
`Unsupported` com o motivo, para a interface mostrar "ainda não processado".
"""
from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from .sources import DOCX, GDOC, GSHEET, MARKDOWN, PDF, XLSX

REGISTER_HEADERS = ["ID", "Atividade", "Responsáveis", "Prazo", "Frente", "Prioridade", "Status",
                    "Próximo passo", "Origem", "Notas e bloqueios"]

META_LINE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.+?)\s*$")


class Unsupported(Exception):
    """Formato fora do escopo implementado (não é erro de leitura)."""


class ExtractionError(Exception):
    """Arquivo no formato esperado, mas ilegível/corrompido."""


@dataclass
class Extracted:
    kind: str                      # "text" ou "table"
    title: str | None = None
    text: str = ""
    meta: dict = field(default_factory=dict)
    sheets: list[dict] = field(default_factory=list)  # [{name, headers, rows:[{row, cells:{col:valor}}]}]

    def canonical(self) -> str:
        """Representação estável usada para o hash de conteúdo (idempotência)."""
        if self.kind == "text":
            return self.text
        import json
        return json.dumps(self.sheets, ensure_ascii=False, sort_keys=True, default=str)


def normalize(s: str) -> str:
    """Minúsculas e sem acento: 'Responsáveis' -> 'responsaveis'."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


# ---------------------------------------------------------------------------
# Texto (Markdown e Google Docs exportado como text/plain)
# ---------------------------------------------------------------------------
def parse_text_document(text: str) -> Extracted:
    # Google Docs exportado pode trazer quebras "moles" (\x0b, \u2028) e espaço não separável
    text = (text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
            .replace("\x0b", "\n").replace("\u2028", "\n").replace("\u2029", "\n").replace("\xa0", " "))
    lines = text.split("\n")
    title = None
    meta: dict[str, str] = {}
    # Cabeçalho: título na primeira linha não vazia; depois linhas "chave: valor"
    # (linhas em branco entre elas são toleradas) até o primeiro parágrafo comum.
    for line in lines[:20]:
        stripped = line.strip()
        if not stripped:
            continue
        if title is None:
            title = stripped.lstrip("#").strip()
            continue
        m = META_LINE.match(stripped)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip("`")
        else:
            break
    return Extracted(kind="text", title=title, text=text, meta=meta)


# ---------------------------------------------------------------------------
# Planilha .xlsx
# ---------------------------------------------------------------------------
def _cell_value(v):
    if isinstance(v, datetime):
        return v.date().isoformat()  # prazo: guardamos só a data
    if isinstance(v, date):
        return v.isoformat()
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip() or None
    return v


def parse_xlsx(data: bytes) -> Extracted:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # arquivo corrompido ou não é xlsx
        raise ExtractionError(f"Não foi possível abrir a planilha: {e}") from e
    sheets = []
    for ws in wb.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            sheets.append({"name": ws.title, "headers": [], "rows": []})
            continue
        headers = [str(h).strip() if h is not None else "" for h in rows[0]]
        data_rows = []
        for i, r in enumerate(rows[1:], start=2):
            cells = {headers[j]: _cell_value(v) for j, v in enumerate(r) if j < len(headers) and headers[j]}
            if any(v is not None for v in cells.values()):
                data_rows.append({"row": i, "cells": cells})
        sheets.append({"name": ws.title, "headers": headers, "rows": data_rows})
    wb.close()
    return Extracted(kind="table", sheets=sheets)


def looks_like_register(sheet: dict) -> bool:
    """Uma aba 'parece registro de atividades' se tem as colunas-chave."""
    heads = {normalize(h) for h in sheet.get("headers", [])}
    return {"id", "atividade", "responsaveis"} <= heads


# ---------------------------------------------------------------------------
# PDF com texto selecionável (diferencial)
# ---------------------------------------------------------------------------
def parse_pdf(data: bytes) -> Extracted:
    import pdfplumber

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pages = [(p.extract_text() or "") for p in pdf.pages]
    except Exception as e:
        raise ExtractionError(f"Não foi possível abrir o PDF: {e}") from e
    text = "\n\n".join(pages).strip()
    if not text:
        raise Unsupported("PDF escaneado, sem texto para ler. O protótipo não lê texto dentro de imagens.")
    return parse_text_document(text)


# ---------------------------------------------------------------------------
def extract(mime_type: str, name: str, data: bytes) -> Extracted:
    if mime_type in (MARKDOWN, GDOC) or name.lower().endswith((".md", ".markdown", ".txt")):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return parse_text_document(text)
    if mime_type in (XLSX, GSHEET) or name.lower().endswith(".xlsx"):
        return parse_xlsx(data)
    if mime_type == PDF or name.lower().endswith(".pdf"):
        return parse_pdf(data)
    if mime_type == DOCX or name.lower().endswith(".docx"):
        raise Unsupported("Arquivo .docx não é lido. No Drive, use Arquivo > Salvar como Documentos Google.")
    raise Unsupported(f"O protótipo ainda não lê este formato ({mime_type}).")


SUPPORTED_HINT = ".md, .xlsx, Google Docs, Google Sheets e PDF com texto"
