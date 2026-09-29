"""Configuração lida de variáveis de ambiente (e do arquivo .env, se existir).

Nenhum segredo fica no código: tudo vem do ambiente.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parent.parent
try:
    TZ = ZoneInfo("America/Sao_Paulo")
except ZoneInfoNotFoundError:
    # Sem base de fusos (Windows sem o pacote tzdata). O Brasil não tem horário de verão
    # desde 2019, então UTC-3 fixo é equivalente para as datas do protótipo.
    TZ = timezone(timedelta(hours=-3), "America/Sao_Paulo")


def _load_dotenv(path: Path) -> None:
    """Carrega KEY=VALUE do .env sem sobrescrever variáveis já definidas no ambiente."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass
class Settings:
    source_mode: str
    local_folder: Path
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    drive_folder_id: str
    llm_provider: str
    gemini_api_key: str
    gemini_model: str
    gemini_fallback_model: str
    gemini_qa_models: str
    discord_webhook_url: str
    app_base_url: str
    session_secret: str
    sync_interval: int
    database_path: Path

    @property
    def drive_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.drive_folder_id
                    and not self.google_client_id.startswith("<"))

    @property
    def discord_enabled(self) -> bool:
        from .notify import valid_webhook
        return valid_webhook(self.discord_webhook_url)

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider == "gemini" and bool(self.gemini_api_key) and not self.gemini_api_key.startswith("<")


def get_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    env = os.environ.get

    def path(value: str) -> Path:
        p = Path(value)
        return p if p.is_absolute() else ROOT / p

    return Settings(
        source_mode=env("SOURCE_MODE", "local"),
        local_folder=path(env("LOCAL_FOLDER", "./amostra")),
        google_client_id=env("GOOGLE_CLIENT_ID", ""),
        google_client_secret=env("GOOGLE_CLIENT_SECRET", ""),
        google_redirect_uri=env("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/callback"),
        drive_folder_id=env("DRIVE_TEST_FOLDER_ID", ""),
        llm_provider=env("LLM_PROVIDER", "off"),
        gemini_api_key=env("GEMINI_API_KEY", ""),
        gemini_model=env("GEMINI_MODEL", "gemini-3.6-flash"),
        gemini_fallback_model=env("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-flash-latest"),
        # Perguntas do "Comece aqui": modelos rápidos primeiro (sem raciocínio interno), tentados em ordem
        gemini_qa_models=env("GEMINI_QA_MODELS", "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.6-flash"),
        discord_webhook_url=env("DISCORD_WEBHOOK_URL", "").strip(),
        app_base_url=env("APP_BASE_URL", "http://localhost:8000").strip(),
        session_secret=env("SESSION_SECRET", "dev-inseguro-troque-no-env"),
        sync_interval=int(env("SYNC_INTERVAL_SECONDS", "180")),
        database_path=path(env("DATABASE_PATH", "./data/central.db")),
    )
