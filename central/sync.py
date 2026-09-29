"""Motor de sincronização: detecta novo / editado / renomeado / removido e processa.

Desenho (o "exemplo simples" da especificação):
  1. Lista a pasta raiz e subpastas (com paginação).
  2. Para cada arquivo, compara o `version` do Drive com o que já vimos.
     Só baixa quando mudou. Renomear muda o nome mas mantém o file_id.
  3. Calcula o hash do CONTEÚDO extraído. Mesmo hash = nada a processar
     (idempotência: o mesmo evento duas vezes não gera nada duplicado).
  4. Arquivos que sumiram da listagem ficam 'indisponível' (não apagamos nada).
  5. Processa na ordem: índice -> registro oficial -> planilhas concorrentes -> atas.

Falha de listagem => a rodada inteira falha e NADA é marcado como removido.
Falha num arquivo => só aquele arquivo fica com erro; o resto segue.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from . import registry
from . import suggestions as sugg
from .authority import classify
from .db import dumps, get_setting, now_iso, set_setting
from . import transcribe
from .extractors import Extracted, ExtractionError, Unsupported, extract
from .issues import open_issue, resolve_issue
from .sources import Source, SourceError, sha256

log = logging.getLogger("central.sync")

PROCESS_ORDER = {"indice": 0, "registro_oficial": 1, "registro_candidato": 2, "estado_atual": 3, "guia": 3,
                 "historico": 3, "outro": 3, "ata": 4}


def _extracted_from_row(v) -> Extracted | None:
    if v is None:
        return None
    if v["extracted_json"]:
        return Extracted(kind="table", sheets=json.loads(v["extracted_json"])["sheets"])
    from .extractors import parse_text_document
    return parse_text_document(v["extracted_text"] or "")


def _ai_backoff_until(conn, file_id: str) -> str | None:
    raw = get_setting(conn, f"ia_backoff:{file_id}")
    return json.loads(raw)["until"] if raw else None


def _register_ai_failure(conn, file_id: str) -> None:
    """Espera crescente entre tentativas automáticas: 3, 6, 12, 24, até 30 min."""
    from datetime import datetime, timedelta

    from .config import TZ
    raw = get_setting(conn, f"ia_backoff:{file_id}")
    fails = (json.loads(raw)["fails"] if raw else 0) + 1
    minutes = min(3 * 2 ** (fails - 1), 30)
    until = (datetime.now(TZ) + timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
    set_setting(conn, f"ia_backoff:{file_id}", json.dumps({"fails": fails, "until": until}))


def reclassify_all(conn: sqlite3.Connection) -> None:
    official = registry.official_register_id(conn)
    for s in conn.execute("SELECT * FROM sources WHERE sync_status = 'ok'").fetchall():
        v = conn.execute("SELECT * FROM source_versions WHERE file_id=? AND content_hash=?",
                         (s["file_id"], s["content_hash"])).fetchone()
        role = classify(s["name"], _extracted_from_row(v), s["file_id"] == official)
        if role != s["role"]:
            conn.execute("UPDATE sources SET role=? WHERE file_id=?", (role, s["file_id"]))


def run_sync(conn: sqlite3.Connection, source: Source, *, trigger: str = "auto", llm=None) -> dict:
    started = now_iso()
    run_id = conn.execute("INSERT INTO sync_runs (trigger, started_at, status) VALUES (?,?, 'rodando')",
                          (trigger, started)).lastrowid
    stats = {"files_seen": 0, "processed": 0, "unchanged": 0, "ignored": 0, "errors": 0}
    messages: list[str] = []

    try:
        files = source.list_tree()
    except SourceError as e:
        conn.execute("UPDATE sync_runs SET status='falhou', finished_at=?, message=? WHERE run_id=?",
                     (now_iso(), f"Não foi possível listar a pasta: {e}. Nenhum dado foi alterado.", run_id))
        return {"run_id": run_id, "status": "falhou", "message": str(e), **stats}

    # Progresso visível enquanto a rodada acontece
    conn.execute("UPDATE sync_runs SET files_seen=?, message=? WHERE run_id=?",
                 (len(files), f"Pasta listada: {len(files)} arquivo(s). Lendo e processando…", run_id))
    changed: list[str] = []
    seen: set[str] = set()
    for f in files:
        seen.add(f.file_id)
        stats["files_seen"] += 1
        ts = now_iso()
        row = conn.execute("SELECT * FROM sources WHERE file_id = ?", (f.file_id,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO sources (file_id, name, mime_type, web_url, path, parent_id, modified_at, sync_status,
                                        first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?, 'pendente', ?, ?)""",
                (f.file_id, f.name, f.mime_type, f.web_url, f.path, f.parent_id, f.modified_at, ts, ts))
        else:
            if row["name"] != f.name:
                messages.append(f"Renomeado: '{row['name']}' → '{f.name}' (mesmo ID, autoridade preservada)")
            conn.execute(
                "UPDATE sources SET name=?, mime_type=?, web_url=?, path=?, parent_id=?, modified_at=?, last_seen_at=? "
                "WHERE file_id=?", (f.name, f.mime_type, f.web_url, f.path, f.parent_id, f.modified_at, ts, f.file_id))
            if row["sync_status"] == "indisponivel":
                conn.execute("UPDATE sources SET sync_status='ok', status_message=NULL WHERE file_id=?", (f.file_id,))
                resolve_issue(conn, f"indisponivel:{f.file_id}", resolution="A fonte voltou a aparecer na pasta")
            same_version = row["drive_version"] == f.version
            if same_version and row["sync_status"] == "nao_suportado" and \
                    transcribe.confirmed_text(conn, f.file_id, f.version) is None:
                stats["ignored"] += 1
                continue
            if same_version and row["sync_status"] in ("ok", "indisponivel") and row["content_hash"]:
                stats["unchanged"] += 1
                if row["last_processed_hash"] != row["content_hash"]:
                    changed.append(f.file_id)  # processamento anterior ficou pendente (ex.: IA fora do ar)
                continue
            # versão nova ou erro anterior: baixa de novo

        try:
            transcript = transcribe.confirmed_text(conn, f.file_id, f.version)
            if transcript is not None:
                # imagem/scan: vale o texto que uma pessoa conferiu com o original (só para esta versão)
                from .extractors import parse_text_document
                ext = parse_text_document(transcript)
            else:
                data = source.fetch(f)
                ext = extract(f.mime_type, f.name, data)
        except Unsupported as u:
            msg = str(u)
            if transcribe.had_older_confirmed(conn, f.file_id, f.version):
                msg += " O arquivo mudou no Drive depois da transcrição conferida: transcreva de novo."
            conn.execute("UPDATE sources SET sync_status='nao_suportado', status_message=?, drive_version=? WHERE file_id=?",
                         (msg, f.version, f.file_id))
            stats["ignored"] += 1
            continue
        except (SourceError, ExtractionError) as e:
            # Erro de leitura NÃO é arquivo vazio: mantemos a última versão boa e avisamos.
            conn.execute("UPDATE sources SET sync_status='erro', status_message=? WHERE file_id=?", (str(e), f.file_id))
            open_issue(conn, "erro_leitura", f"Erro ao ler {f.name}",
                       f"{e}. A última versão lida com sucesso (se houver) continua valendo, marcada como possivelmente desatualizada.",
                       dedupe_key=f"erro:{f.file_id}", file_id=f.file_id)
            stats["errors"] += 1
            continue

        content_hash = sha256(ext.canonical().encode("utf-8"))
        conn.execute(
            """INSERT OR IGNORE INTO source_versions (file_id, drive_version, content_hash, modified_at, name,
                                                      extracted_text, extracted_json, fetched_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (f.file_id, f.version, content_hash, f.modified_at, f.name,
             ext.text if ext.kind == "text" else None,
             dumps({"sheets": ext.sheets}) if ext.kind == "table" else None, now_iso()))
        conn.execute(
            "UPDATE sources SET drive_version=?, content_hash=?, sync_status='ok', status_message=NULL, doc_meta=?, "
            "doc_status=? WHERE file_id=?",
            (f.version, content_hash, dumps(ext.meta), ext.meta.get("status"), f.file_id))
        resolve_issue(conn, f"erro:{f.file_id}", resolution="Arquivo lido com sucesso na sincronização seguinte")
        prev_processed = row["last_processed_hash"] if row else None
        if prev_processed == content_hash:
            stats["unchanged"] += 1  # só metadados mudaram (ex.: renomeação)
            continue
        changed.append(f.file_id)

    # Removidos ou sem acesso: marca indisponível, não apaga
    for row in conn.execute("SELECT * FROM sources WHERE sync_status <> 'indisponivel'").fetchall():
        if row["file_id"] not in seen:
            conn.execute("UPDATE sources SET sync_status='indisponivel', status_message=? WHERE file_id=?",
                         ("Não aparece mais na pasta monitorada (removido, movido ou sem acesso).", row["file_id"]))
            open_issue(conn, "fonte_indisponivel", f"Fonte indisponível: {row['name']}",
                       "O arquivo não aparece mais na pasta. Informações que dependem dele continuam visíveis, "
                       "marcadas como possivelmente desatualizadas. Nenhuma atividade foi alterada.",
                       dedupe_key=f"indisponivel:{row['file_id']}", file_id=row["file_id"])
            n = sugg.mark_stale_for_file(conn, row["file_id"], None, "Fonte removida ou sem acesso")
            if n:
                messages.append(f"{n} sugestão(ões) de '{row['name']}' marcadas como desatualizadas")

    # Processamento
    reclassify_all(conn)
    registry.ensure_register_bound(conn)
    reclassify_all(conn)
    roles = {r["file_id"]: (r["role"], r["name"]) for r in conn.execute("SELECT file_id, role, name FROM sources")}
    # Ordem determinística: papel (índice e registro primeiro), depois nome
    for file_id in sorted(set(changed), key=lambda i: (PROCESS_ORDER.get(roles[i][0], 3), roles[i][1])):
        role = roles[file_id][0]
        name = conn.execute("SELECT name, content_hash FROM sources WHERE file_id=?", (file_id,)).fetchone()
        try:
            if role == "registro_oficial":
                msg = registry.process_official_register(conn, file_id)
            elif role == "registro_candidato":
                msg = registry.process_register_candidate(conn, file_id)
            elif role == "ata":
                n = sugg.mark_stale_for_file(conn, file_id, name["content_hash"],
                                             "A ata foi editada; esta sugestão era da versão anterior")
                if n:
                    messages.append(f"{name['name']}: {n} sugestão(ões) da versão anterior marcadas como desatualizadas")
                if llm is None:
                    conn.execute("UPDATE sources SET status_message=? WHERE file_id=?",
                                 ("Lida, mas a IA está desligada: sem sugestões por enquanto.", file_id))
                    continue  # não marca como processada: será analisada quando a IA estiver ativa
                from .ai import analyze_minutes
                wait_until = _ai_backoff_until(conn, file_id)
                if wait_until and trigger == "auto" and now_iso() < wait_until:
                    # A IA falhou há pouco para este arquivo: espaça as tentativas automáticas
                    # (o botão "Sincronizar agora" ignora essa espera).
                    conn.execute("UPDATE sources SET status_message=? WHERE file_id=?",
                                 (f"IA indisponível na última tentativa. Nova tentativa automática após "
                                  f"{wait_until[11:16]}, ou use 'Sincronizar agora'.", file_id))
                    continue
                conn.execute("UPDATE sync_runs SET message=? WHERE run_id=?",
                             (f"Analisando {name['name']} com a IA…", run_id))
                msg = analyze_minutes(conn, llm, file_id)
                set_setting(conn, f"ia_backoff:{file_id}", None)
            else:
                msg = "Lido como referência"
        except Exception as e:  # falha isolada não derruba a rodada
            from .ai import LLMError
            if isinstance(e, LLMError):
                _register_ai_failure(conn, file_id)
                log.warning("IA indisponível para %s: %s", name["name"], e)
                msg = (f"Documento lido, mas a IA não respondeu ({e}). "
                       "Nenhuma sugestão foi criada; nova tentativa na próxima sincronização.")
            else:
                log.exception("Falha ao processar %s", file_id)
                msg = f"Falha ao processar: {e}"
            conn.execute("UPDATE sources SET status_message=? WHERE file_id=?", (msg, file_id))
            messages.append(f"{name['name']}: {msg}")
            stats["errors"] += 1
            continue
        conn.execute("UPDATE sources SET last_processed_hash=content_hash, last_processed_at=?, status_message=? "
                     "WHERE file_id=?", (now_iso(), msg, file_id))
        stats["processed"] += 1
        messages.append(f"{name['name']}: {msg}")

    status = "ok" if stats["errors"] == 0 else "parcial"
    conn.execute(
        "UPDATE sync_runs SET status=?, finished_at=?, files_seen=?, processed=?, unchanged=?, ignored=?, errors=?, "
        "message=? WHERE run_id=?",
        (status, now_iso(), stats["files_seen"], stats["processed"], stats["unchanged"], stats["ignored"],
         stats["errors"], "\n".join(messages) or "Nenhuma mudança", run_id))
    return {"run_id": run_id, "status": status, "messages": messages, **stats}
