"""Banco SQLite: esquema, conexão e utilitários de tempo.

Escolha: SQLite num único arquivo. Persiste entre reinícios, não exige servidor
e é suficiente para um protótipo com uma pasta de teste pequena.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from .config import TZ

SCHEMA = """
PRAGMA foreign_keys = ON;

-- Pessoas de demonstração (fictícias)
CREATE TABLE IF NOT EXISTS members (
    member_id     TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    front         TEXT NOT NULL,
    role          TEXT NOT NULL,
    can_review    INTEGER NOT NULL DEFAULT 0
);

-- Configurações e estado interno (ex.: qual file_id é o registro oficial)
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Fonte: um arquivo do Drive, identificado pelo file_id estável (não pelo nome)
CREATE TABLE IF NOT EXISTS sources (
    file_id            TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    mime_type          TEXT NOT NULL,
    web_url            TEXT,
    path               TEXT,
    parent_id          TEXT,
    modified_at        TEXT,
    drive_version      TEXT,
    content_hash       TEXT,           -- hash do conteúdo extraído da versão atual
    sync_status        TEXT NOT NULL,  -- ok | erro | indisponivel | nao_suportado
    status_message     TEXT,
    role               TEXT,           -- indice | estado_atual | guia | ata | registro_oficial | registro_candidato | historico | outro
    doc_status         TEXT,           -- status declarado no cabeçalho (ativo, parcial, deprecated...)
    doc_meta           TEXT,           -- JSON com o cabeçalho do documento
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    last_processed_at  TEXT,
    last_processed_hash TEXT
);

-- Cada versão de conteúdo lida de uma fonte (base para "o que mudou" e idempotência)
CREATE TABLE IF NOT EXISTS source_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         TEXT NOT NULL REFERENCES sources(file_id),
    drive_version   TEXT,
    content_hash    TEXT NOT NULL,
    modified_at     TEXT,
    name            TEXT,
    extracted_text  TEXT,
    extracted_json  TEXT,
    fetched_at      TEXT NOT NULL,
    UNIQUE (file_id, content_hash)
);

CREATE TABLE IF NOT EXISTS activities (
    activity_id    TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    description    TEXT,
    next_step      TEXT,
    front          TEXT,
    priority       TEXT,
    status         TEXT NOT NULL,
    due_date       TEXT,             -- ISO AAAA-MM-DD ou NULL ("a definir")
    notes          TEXT,
    origin_label   TEXT,             -- texto da coluna "Origem" da planilha
    creation_kind  TEXT NOT NULL,    -- importacao | manual | sugestao
    created_by     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

-- N:N para permitir mais de um responsável (ex.: ACT-104)
CREATE TABLE IF NOT EXISTS activity_owners (
    activity_id  TEXT NOT NULL REFERENCES activities(activity_id),
    member_id    TEXT NOT NULL REFERENCES members(member_id),
    PRIMARY KEY (activity_id, member_id)
);

-- Referência: qual documento sustentou a criação ou alteração
CREATE TABLE IF NOT EXISTS activity_refs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id      TEXT NOT NULL REFERENCES activities(activity_id),
    file_id          TEXT NOT NULL,
    version_or_hash  TEXT,
    sheet_or_section TEXT,
    quote_or_cell    TEXT,
    relation_type    TEXT NOT NULL,  -- importada_de | origem_declarada | alterada_por | criada_por
    created_at       TEXT NOT NULL
);

-- Evento de atividade: histórico auditável (antes/depois, quem, por quê)
CREATE TABLE IF NOT EXISTS activity_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id     TEXT NOT NULL REFERENCES activities(activity_id),
    actor_id        TEXT NOT NULL,
    ts              TEXT NOT NULL,
    before_json     TEXT,
    after_json      TEXT,
    reason          TEXT,
    source_file_id  TEXT,
    source_version  TEXT,
    suggestion_id   INTEGER
);

-- Sugestões da IA: separadas do registro oficial até revisão humana
CREATE TABLE IF NOT EXISTS suggestions (
    suggestion_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind               TEXT NOT NULL,       -- create | update
    target_activity_id TEXT,
    proposed_fields    TEXT NOT NULL,       -- JSON
    current_fields     TEXT,                -- JSON: valores oficiais no momento da sugestão
    evidence           TEXT NOT NULL,
    reason             TEXT,
    doc_date           TEXT,
    uncertainties      TEXT,                -- JSON (lista)
    source_file_id     TEXT NOT NULL,
    source_version     TEXT NOT NULL,       -- hash do conteúdo analisado
    review_status      TEXT NOT NULL,       -- pendente | aceita | aceita_com_ajuste | rejeitada | desatualizada
    reviewer_id        TEXT,
    reviewed_at        TEXT,
    review_note        TEXT,
    model              TEXT,
    created_at         TEXT NOT NULL,
    dedupe_key         TEXT NOT NULL UNIQUE,
    already_fields     TEXT,                -- JSON: o que o documento também diz, mas o quadro já tem
    applied_fields     TEXT                 -- JSON: o que foi aplicado ao aceitar (com ajuste, difere do sugerido)
);

-- O que a extração viu mas NÃO virou sugestão (hipóteses, itens barrados pela validação)
CREATE TABLE IF NOT EXISTS extraction_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         TEXT NOT NULL,
    source_version  TEXT NOT NULL,
    kind            TEXT NOT NULL,   -- hipotese | barrada_validacao | sem_mudanca | instrucao_ignorada | precisa_conferir
    text            TEXT,
    reason          TEXT,
    created_at      TEXT NOT NULL
);

-- Pendências humanas: conflitos, fontes indisponíveis, erros de leitura
CREATE TABLE IF NOT EXISTS issues (
    issue_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,   -- registro_homonimo | fonte_indisponivel | erro_leitura | registro_nao_definido | responsavel_desconhecido
    file_id      TEXT,
    title        TEXT NOT NULL,
    detail       TEXT,
    status       TEXT NOT NULL,   -- aberta | resolvida
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    resolved_by  TEXT,
    resolution   TEXT,
    dedupe_key   TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger      TEXT NOT NULL,   -- auto | manual | teste
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,   -- rodando | ok | parcial | falhou
    files_seen   INTEGER DEFAULT 0,
    processed    INTEGER DEFAULT 0,
    unchanged    INTEGER DEFAULT 0,
    ignored      INTEGER DEFAULT 0,
    errors       INTEGER DEFAULT 0,
    message      TEXT
);

-- Transcrição de imagem/PDF escaneado pedida por uma pessoa: rascunho da IA, conferido antes de valer
CREATE TABLE IF NOT EXISTS transcriptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       TEXT NOT NULL,
    drive_version TEXT,               -- vale só para esta versão do arquivo
    ai_text       TEXT NOT NULL,      -- o que a IA leu
    text          TEXT NOT NULL,      -- o que a pessoa confirmou (pode ter correções)
    note          TEXT,               -- observação da IA (ex.: trechos ilegíveis)
    status        TEXT NOT NULL,      -- rascunho | confirmada | descartada
    model         TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    confirmed_by  TEXT,
    confirmed_at  TEXT
);

-- Cada chamada ao modelo: base para a estimativa de custo e para depuração
CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    purpose         TEXT NOT NULL,   -- extracao_ata | resumo
    file_id         TEXT,
    source_version  TEXT,
    model           TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    ok              INTEGER NOT NULL,
    error           TEXT,
    raw_output      TEXT
);

CREATE TABLE IF NOT EXISTS member_visits (
    member_id      TEXT PRIMARY KEY,
    last_visit_at  TEXT,
    prev_visit_at  TEXT
);
"""

# Pessoas de demonstração definidas no LEIA_ME_PRIMEIRO.md do pacote de testes
DEMO_MEMBERS = [
    ("U-A", "Ana", "Growth", "Membro, responsável por tarefas", 0),
    ("U-B", "Bruno", "Growth", "Líder de Growth, revisa sugestões", 1),
    ("U-C", "Carla", "Formação", "Membro e revisora de sugestões", 1),
    ("U-D", "Davi", "Operações", "Membro, responsável por tarefas", 0),
]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL") if str(path) != ":memory:" else None
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # Bancos criados antes de uma coluna existir ganham a coluna (sem perder dados)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(suggestions)")}
    if "already_fields" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN already_fields TEXT")
    if "applied_fields" not in cols:
        conn.execute("ALTER TABLE suggestions ADD COLUMN applied_fields TEXT")
    conn.executemany(
        "INSERT OR IGNORE INTO members (member_id, display_name, front, role, can_review) VALUES (?,?,?,?,?)",
        DEMO_MEMBERS,
    )


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


class transaction:
    """Contexto de transação explícita (a conexão usa autocommit)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False
