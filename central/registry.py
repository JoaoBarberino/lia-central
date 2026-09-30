"""Registro oficial de atividades vindo da planilha.

1. Vínculo: o INDEX.md aponta a planilha pelo nome; no primeiro vínculo guardamos
   o file_id do Drive. A partir daí o NOME não importa: renomear mantém a
   autoridade; uma planilha nova com o mesmo nome (outro file_id) não herda nada.
2. Importação inicial: cria as atividades uma única vez, com referência à célula.
3. Edição posterior da planilha oficial: comparamos a versão nova com a
   versão ANTERIOR da planilha (não com o banco). Só células que mudaram na
   planilha viram sugestões. Assim uma planilha desatualizada não desfaz uma
   decisão aprovada na aplicação.
4. Linhas que somem da planilha NÃO apagam atividades: viram pendência.
5. Outra planilha com formato de registro (ex.: 'Ata - copia vazia.xlsx') não
   substitui nada: vira pendência visível de conflito.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from . import activities as acts
from . import suggestions as sugg
from .authority import find_register_pointer
from .db import get_setting, set_setting
from .extractors import looks_like_register, normalize
from .issues import open_issue, resolve_issue

COLUMN_TO_FIELD = {
    "id": "activity_id", "atividade": "title", "responsaveis": "owners", "prazo": "due_date", "frente": "front",
    "prioridade": "priority", "status": "status", "proximo passo": "next_step", "origem": "origin_label",
    "notas e bloqueios": "notes", "descricao": "description",
}


def _col_letter(idx: int) -> str:
    s = ""
    idx += 1
    while idx:
        idx, r = divmod(idx - 1, 26)
        s = chr(65 + r) + s
    return s


BLANK_DATES = {"", "-", "—", "a definir", "sem prazo", "indefinido", "tbd", "n/a"}


def parse_due(value) -> tuple[str | None, bool]:
    """Prazo de uma célula: (data ISO ou None, ok). Aceita data do Excel, AAAA-MM-DD e dd/mm/aaaa (ou dd/mm/aa).
    Texto que não é data (ex.: "sexta") volta como (None, False): vira "a definir" e uma pendência."""
    if value is None:
        return None, True
    text = str(value).strip()
    if normalize(text) in BLANK_DATES:
        return None, True
    if acts.valid_iso_date(text[:10]) and (len(text) == 10 or text[10] in " T"):
        return text[:10], True
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})", text)
    if m:
        day, month, year = int(m[1]), int(m[2]), int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        try:
            return date(year, month, day).isoformat(), True
        except ValueError:
            pass
    return None, False


def row_to_fields(conn, sheet: dict, row: dict, raw: dict | None = None,
                  bad: dict | None = None) -> tuple[dict, list[str]]:
    """Converte uma linha da planilha em campos da atividade. Devolve também nomes não reconhecidos.
    `raw` recebe o texto original de cada campo (para a evidência); `bad`, os campos com valor ilegível."""
    fields: dict = {}
    unknown: list[str] = []
    for header, value in row["cells"].items():
        key = COLUMN_TO_FIELD.get(normalize(header))
        if not key:
            continue
        if raw is not None:
            raw[key] = value
        if key == "owners":
            fields["owners"], unknown = acts.parse_owner_names(conn, value)
        elif key == "status":
            fields["status"] = acts.normalize_status(value)
        elif key == "due_date":
            fields["due_date"], ok = parse_due(value)
            if not ok and bad is not None:
                bad["due_date"] = value
        else:
            fields[key] = str(value) if value is not None else None
    return fields, unknown


def cell_ref(sheet: dict, row: dict, field: str) -> str:
    for j, h in enumerate(sheet["headers"]):
        if COLUMN_TO_FIELD.get(normalize(h)) == field:
            return f"{sheet['name']}!{_col_letter(j)}{row['row']}"
    return f"{sheet['name']}!linha {row['row']}"


def _pick_sheet(sheets: list[dict], wanted: str | None) -> dict | None:
    if wanted:
        for s in sheets:
            if normalize(s["name"]) == normalize(wanted):
                return s
        return None
    regs = [s for s in sheets if looks_like_register(s)]
    return regs[0] if len(regs) == 1 else None


def _latest_version(conn, file_id):
    return conn.execute(
        "SELECT sv.* FROM sources s JOIN source_versions sv ON sv.file_id = s.file_id AND sv.content_hash = s.content_hash "
        "WHERE s.file_id = ?", (file_id,)).fetchone()


def official_register_id(conn) -> str | None:
    return get_setting(conn, "register_file_id")


# ---------------------------------------------------------------------------
def ensure_register_bound(conn: sqlite3.Connection) -> str | None:
    """Garante que existe um registro oficial vinculado por file_id. Retorna o file_id ou None."""
    bound = official_register_id(conn)
    if bound:
        return bound
    index = conn.execute(
        "SELECT s.file_id, sv.extracted_text FROM sources s JOIN source_versions sv "
        "ON sv.file_id = s.file_id AND sv.content_hash = s.content_hash "
        "WHERE s.role = 'indice' AND s.sync_status = 'ok'").fetchall()
    pointer = None
    for r in index:
        pointer = find_register_pointer(r["extracted_text"] or "")
        if pointer:
            break
    if not pointer:
        open_issue(conn, "registro_nao_definido", "Registro oficial de atividades não definido",
                   "Nenhum INDEX.md ativo aponta uma planilha como fonte das atividades. Nenhuma atividade foi importada.",
                   dedupe_key="registro_nao_definido")
        return None
    name, sheet = pointer
    candidates = conn.execute(
        "SELECT file_id, name FROM sources WHERE name = ? AND sync_status = 'ok'", (name,)).fetchall()
    found_as = name
    if not candidates:
        # Mesmo nome com outra extensão de planilha (ex.: o INDEX diz Ata_registro.xlsx e a planilha foi salva
        # como Ata_registro.xlsm, ou convertida para Planilhas Google, sem extensão): aceita se houver só uma.
        stem = re.sub(r"\.xls[xm]$", "", name, flags=re.IGNORECASE)
        candidates = [r for r in conn.execute(
            "SELECT file_id, name FROM sources WHERE sync_status = 'ok' AND (name = ? OR name = ? OR name = ?)",
            (f"{stem}.xlsx", f"{stem}.xlsm", stem)).fetchall()]
        if len(candidates) == 1:
            found_as = candidates[0]["name"]
    if len(candidates) != 1:
        detail = (f"O INDEX.md aponta '{name}', mas "
                  + ("nenhum arquivo com esse nome foi encontrado." if not candidates
                     else f"existem {len(candidates)} arquivos com esse nome. Não é seguro escolher sozinho."))
        open_issue(conn, "registro_nao_definido", "Registro oficial de atividades não definido", detail,
                   dedupe_key="registro_nao_definido")
        return None
    file_id = candidates[0]["file_id"]
    set_setting(conn, "register_file_id", file_id)
    set_setting(conn, "register_sheet", sheet or "")
    set_setting(conn, "register_bound_reason", f"Apontado pelo INDEX.md como '{name}'" + (f", aba '{sheet}'" if sheet else "")
                + (f"; na pasta ela está como '{found_as}' (mesmo nome, outra extensão de planilha)" if found_as != name else ""))
    resolve_issue(conn, "registro_nao_definido", resolution="Registro vinculado pelo INDEX.md")
    return file_id


def process_official_register(conn: sqlite3.Connection, file_id: str) -> str:
    """Importa (1ª vez) ou compara a versão nova com a anterior. Retorna um resumo."""
    version = _latest_version(conn, file_id)
    if version is None:
        return "sem versão legível"
    data = json.loads(version["extracted_json"])
    sheet = _pick_sheet(data["sheets"], get_setting(conn, "register_sheet") or None)
    if sheet is None:
        open_issue(conn, "registro_alterado", "Aba de atividades não encontrada no registro oficial",
                   f"A aba esperada ('{get_setting(conn, 'register_sheet')}') não existe na versão atual. "
                   "Nenhuma atividade foi alterada.", dedupe_key=f"registro_sem_aba:{file_id}", file_id=file_id)
        return "aba não encontrada"
    baseline_hash = get_setting(conn, "register_baseline_hash")
    if baseline_hash is None:
        return _initial_import(conn, file_id, version["content_hash"], sheet)
    if baseline_hash == version["content_hash"]:
        return "sem mudanças"
    base = conn.execute("SELECT extracted_json FROM source_versions WHERE file_id=? AND content_hash=?",
                        (file_id, baseline_hash)).fetchone()
    base_sheet = _pick_sheet(json.loads(base["extracted_json"])["sheets"], get_setting(conn, "register_sheet") or None) if base else None
    summary = _diff_to_suggestions(conn, file_id, version["content_hash"], base_sheet or {"rows": [], "headers": []}, sheet)
    set_setting(conn, "register_baseline_hash", version["content_hash"])
    return summary


def _bad_date_issue(conn, file_id, sheet, row, act_id, value) -> None:
    open_issue(conn, "registro_alterado", f"Prazo ilegível na planilha oficial ({act_id})",
               f"{cell_ref(sheet, row, 'due_date')} tem {value!r}, que não é uma data. "
               "O prazo ficou 'a definir'; corrija a célula ou edite a atividade.",
               dedupe_key=f"prazo_ilegivel:{act_id}:{value}", file_id=file_id)


def _initial_import(conn, file_id, content_hash, sheet) -> str:
    count = 0
    for row in sheet["rows"]:
        try:   # uma linha com problema não impede as outras (falha isolada nunca vira ausência)
            count += _import_row(conn, file_id, content_hash, sheet, row)
        except Exception as e:
            open_issue(conn, "registro_alterado", f"Linha {row['row']} da planilha oficial não foi importada",
                       f"{sheet['name']}!linha {row['row']}: {e}. As demais linhas foram importadas.",
                       dedupe_key=f"linha_nao_importada:{file_id}:{row['row']}", file_id=file_id)
    set_setting(conn, "register_baseline_hash", content_hash)
    return f"{count} atividades importadas"


def _import_row(conn, file_id, content_hash, sheet, row) -> int:
    bad: dict = {}
    fields, unknown = row_to_fields(conn, sheet, row, bad=bad)
    act_id = fields.pop("activity_id", None)
    if not act_id or not fields.get("title"):
        return 0
    origin = fields.pop("origin_label", None)
    if acts.snapshot(conn, act_id):
        return 0  # idempotente
    if unknown:
        open_issue(conn, "responsavel_desconhecido", f"Responsável não reconhecido em {act_id}",
                   f"Nomes não encontrados entre os membros: {', '.join(unknown)}. Mostrando 'responsável a confirmar'.",
                   dedupe_key=f"resp:{act_id}", file_id=file_id)
    if "due_date" in bad:
        _bad_date_issue(conn, file_id, sheet, row, act_id, bad["due_date"])
    acts.create_activity(conn, fields, actor_id="sistema", creation_kind="importacao", activity_id=act_id,
                         origin_label=origin, reason=f"Importação inicial do registro oficial ({sheet['name']}, linha {row['row']})",
                         source_file_id=file_id, source_version=content_hash)
    acts.add_ref(conn, act_id, file_id, content_hash, f"{sheet['name']}!linha {row['row']}",
                 " | ".join(f"{k}: {v}" for k, v in row["cells"].items() if v is not None), "importada_de")
    if origin:
        src = conn.execute("SELECT file_id FROM sources WHERE name = ?", (origin,)).fetchone()
        if src:
            acts.add_ref(conn, act_id, src["file_id"], None, None, None, "origem_declarada")
    return 1


def _diff_to_suggestions(conn, file_id, new_hash, old_sheet, new_sheet) -> str:
    old_rows, old_raw = {}, {}
    for r in old_sheet["rows"]:
        raw: dict = {}
        f, _ = row_to_fields(conn, old_sheet, r, raw=raw)
        if f.get("activity_id"):
            old_rows[f["activity_id"]] = f
            old_raw[f["activity_id"]] = raw
    created = removed = 0
    new_ids = set()
    for row in new_sheet["rows"]:
        raw, bad = {}, {}
        fields, unknown = row_to_fields(conn, new_sheet, row, raw=raw, bad=bad)
        act_id = fields.pop("activity_id", None)
        fields.pop("origin_label", None)
        if not act_id:
            continue
        new_ids.add(act_id)
        old = dict(old_rows.get(act_id) or {})
        old.pop("activity_id", None)
        old.pop("origin_label", None)
        existing = acts.snapshot(conn, act_id)
        if act_id not in old_rows and existing:
            kind = conn.execute("SELECT creation_kind FROM activities WHERE activity_id=?", (act_id,)).fetchone()[0]
            if kind != "importacao":
                # O mesmo código já é de uma atividade criada na Central: não sobrescreve, pede decisão
                open_issue(conn, "registro_alterado", f"Código {act_id} em conflito",
                           f"A planilha oficial ganhou uma linha {act_id} (\"{fields.get('title') or ''}\"), "
                           f"mas {act_id} já é a atividade \"{existing['title']}\", criada na Central. "
                           "Nada foi alterado; use outro código na planilha ou crie a atividade pela Central.",
                           dedupe_key=f"id_conflito:{act_id}:{new_hash}", file_id=file_id)
                continue
        if "due_date" in bad:   # prazo ilegível: não apaga o prazo atual, pede correção
            fields.pop("due_date", None)
            _bad_date_issue(conn, file_id, new_sheet, row, act_id, bad["due_date"])
        uncert = []
        if unknown:
            uncert.append(f"Nome não reconhecido entre os membros: {', '.join(unknown)}. Responsável a confirmar.")
            open_issue(conn, "responsavel_desconhecido", f"Responsável não reconhecido em {act_id}",
                       f"{cell_ref(new_sheet, row, 'owners')} tem {raw.get('owners')!r}; "
                       f"não são membros: {', '.join(unknown)}.",
                       dedupe_key=f"resp:{act_id}:{new_hash}", file_id=file_id)
        if act_id not in old_rows and not existing:
            if sugg.create_suggestion(conn, kind="create", target_activity_id=None, proposed=fields,
                                      evidence=f"{new_sheet['name']}!linha {row['row']}",
                                      reason="Nova linha no registro oficial", source_file_id=file_id,
                                      source_version=new_hash, uncertainties=uncert or None):
                created += 1
            continue
        changed = {k: v for k, v in fields.items() if old.get(k) != v}
        if changed:
            # evidência = o texto das células, como está na planilha (não os códigos internos)
            prev = old_raw.get(act_id, {})
            evidence = "; ".join(f"{cell_ref(new_sheet, row, k)}: {_cell_text(prev.get(k))} → {_cell_text(raw.get(k))}"
                                 for k in changed)
            if sugg.create_suggestion(conn, kind="update", target_activity_id=act_id, proposed=changed,
                                      evidence=evidence, reason="Célula alterada no registro oficial",
                                      source_file_id=file_id, source_version=new_hash,
                                      uncertainties=uncert or None):
                created += 1
    missing = sorted(set(old_rows) - new_ids)
    if missing:
        removed = len(missing)
        open_issue(conn, "registro_alterado", "Linhas sumiram do registro oficial",
                   f"A versão nova da planilha oficial não tem: {', '.join(missing)}. "
                   "Nenhuma atividade foi apagada. Confira se a remoção foi intencional e conclua as atividades pela aplicação.",
                   dedupe_key=f"registro_linhas_ausentes:{file_id}:{new_hash}", file_id=file_id)
    msg = f"{created} {'sugestão criada' if created == 1 else 'sugestões criadas'} a partir da planilha"
    if removed:
        msg += f"; {removed} {'linha sumiu e virou pendência' if removed == 1 else 'linhas sumiram e viraram pendência'}"
    return msg


def _cell_text(v) -> str:
    return "(vazia)" if v in (None, "") else repr(str(v))


def process_register_candidate(conn: sqlite3.Connection, file_id: str) -> str:
    """Planilha com formato de registro que NÃO é a oficial: nunca altera atividades."""
    src = conn.execute("SELECT * FROM sources WHERE file_id = ?", (file_id,)).fetchone()
    version = _latest_version(conn, file_id)
    official_id = official_register_id(conn)
    official = conn.execute("SELECT name FROM sources WHERE file_id = ?", (official_id,)).fetchone() if official_id else None
    detail = ("Tem o formato do quadro de atividades, mas não é a planilha indicada no INDEX.md"
              + (f" ({official['name']})" if official else "") + ". Nada foi alterado."
              + (f" Para mudar o quadro, edite o próprio {official['name']} (no Drive: botão direito → Gerenciar "
                 "versões → Enviar nova versão mantém o mesmo arquivo); um arquivo novo, mesmo com nome parecido, "
                 "não herda a autoridade." if official else ""))
    key = f"homonimo:{file_id}:{version['content_hash']}"
    # Pendência de versão anterior do mesmo arquivo é substituída pela atual
    conn.execute("UPDATE issues SET status='resolvida', resolution='Substituída pela análise da versão nova' "
                 "WHERE kind='registro_homonimo' AND file_id=? AND dedupe_key<>? AND status='aberta'", (file_id, key))
    open_issue(conn, "registro_homonimo", f"Planilha concorrente: {src['name']}", detail,
               dedupe_key=key, file_id=file_id)
    return "Conflito registrado em Pendências"
