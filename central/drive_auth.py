"""OAuth 2.0 de aplicação web (authorization code flow) com escopo somente leitura.

- O segredo do cliente fica no .env (servidor), nunca no navegador.
- O parâmetro `state` é validado no callback (proteção contra CSRF).
- O token fica no banco local (data/, fora do Git). Em produção, o certo seria um
  gerenciador de segredos ou criptografia em repouso — documentado no README.
"""
from __future__ import annotations

import json
import os
import sqlite3

from .config import Settings
from .db import get_setting, set_setting

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class DriveNotConnected(Exception):
    pass


def _client_config(s: Settings) -> dict:
    return {"web": {
        "client_id": s.google_client_id,
        "client_secret": s.google_client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": [s.google_redirect_uri],
    }}


def _allow_http_localhost(s: Settings) -> None:
    # oauthlib exige HTTPS; em localhost o Google permite http. Só liberamos nesse caso.
    if s.google_redirect_uri.startswith("http://localhost") or s.google_redirect_uri.startswith("http://127.0.0.1"):
        os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def authorization_url(s: Settings) -> tuple[str, str, str | None]:
    from google_auth_oauthlib.flow import Flow

    _allow_http_localhost(s)
    flow = Flow.from_client_config(_client_config(s), scopes=SCOPES, redirect_uri=s.google_redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="false")
    return url, state, getattr(flow, "code_verifier", None)


def finish_authorization(conn: sqlite3.Connection, s: Settings, full_callback_url: str, state: str,
                         code_verifier: str | None) -> None:
    from google_auth_oauthlib.flow import Flow

    _allow_http_localhost(s)
    flow = Flow.from_client_config(_client_config(s), scopes=SCOPES, redirect_uri=s.google_redirect_uri, state=state)
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(authorization_response=full_callback_url)
    set_setting(conn, "google_token", flow.credentials.to_json())


def load_credentials(conn: sqlite3.Connection, s: Settings):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    raw = get_setting(conn, "google_token")
    if not raw:
        raise DriveNotConnected("Drive não conectado. Use 'Conectar Google Drive'.")
    creds = Credentials.from_authorized_user_info(json.loads(raw), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as e:
                set_setting(conn, "google_token", None)
                raise DriveNotConnected(
                    "A autorização do Drive expirou ou foi revogada (apps em modo Testing expiram em 7 dias). "
                    "Conecte de novo.") from e
            set_setting(conn, "google_token", creds.to_json())
        else:
            raise DriveNotConnected("Credenciais do Drive inválidas. Conecte de novo.")
    return creds


def disconnect(conn: sqlite3.Connection) -> None:
    """Revoga no Google (se possível) e apaga o token local."""
    raw = get_setting(conn, "google_token")
    if raw:
        try:
            import httpx
            token = json.loads(raw).get("refresh_token") or json.loads(raw).get("token")
            httpx.post("https://oauth2.googleapis.com/revoke", params={"token": token}, timeout=10)
        except Exception:
            pass
    set_setting(conn, "google_token", None)
