"""Fontes de documentos: Google Drive (real) e pasta local (desenvolvimento e testes).

As duas implementam a mesma interface, então o motor de sincronização não sabe
de onde vêm os arquivos. Isso permite testar todos os cenários (arquivo novo,
edição, renomeação, remoção, erro) sem depender da rede.
"""
from __future__ import annotations

import hashlib
import mimetypes
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
FOLDER = "application/vnd.google-apps.folder"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GSLIDES = "application/vnd.google-apps.presentation"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
CSV = "text/csv"
TEXT = "text/plain"
MARKDOWN = "text/markdown"
PDF = "application/pdf"

# Limite documentado pela Drive API para files.export
EXPORT_LIMIT_BYTES = 10 * 1024 * 1024


class SourceError(Exception):
    """Falha ao listar ou ler. NUNCA deve ser interpretada como 'arquivo vazio'."""

    def __init__(self, message: str, temporary: bool = False):
        super().__init__(message)
        self.temporary = temporary


@dataclass
class RemoteFile:
    file_id: str
    name: str
    mime_type: str
    modified_at: str | None
    version: str | None
    md5: str | None
    web_url: str | None
    path: str
    parent_id: str | None
    can_download: bool = True


class Source(Protocol):
    kind: str

    def list_tree(self) -> list[RemoteFile]: ...

    def fetch(self, f: RemoteFile) -> bytes: ...


# ---------------------------------------------------------------------------
# Pasta local
# ---------------------------------------------------------------------------
class LocalSource:
    """Simula o Drive com uma pasta do computador.

    - file_id = número do inode, que se mantém quando o arquivo é renomeado
      (igual ao ID do Drive).
    - Arquivos terminados em ".gdoc" são tratados como Google Docs nativos
      (conteúdo em texto puro), para testar esse caminho sem rede.
    """

    kind = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def list_tree(self) -> list[RemoteFile]:
        if not self.root.is_dir():
            raise SourceError(f"Pasta local não encontrada: {self.root}")
        out: list[RemoteFile] = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            st = p.stat()
            rel = p.relative_to(self.root)
            if p.suffix == ".gdoc":
                mime = GDOC
                name = p.stem
            else:
                name = p.name
                mime = {".md": MARKDOWN, ".xlsx": XLSX, ".docx": DOCX, ".pdf": PDF, ".pptx": PPTX, ".csv": CSV,
                        ".txt": TEXT}.get(
                    p.suffix.lower(), mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                )
            out.append(
                RemoteFile(
                    file_id=f"local-{st.st_ino}",
                    name=name,
                    mime_type=mime,
                    modified_at=datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    version=f"{st.st_mtime_ns}-{st.st_size}",
                    md5=None,
                    web_url=p.resolve().as_uri(),
                    path="/" + str(rel.parent).replace("\\", "/").strip("."),
                    parent_id=str(rel.parent),
                )
            )
        return out

    def fetch(self, f: RemoteFile) -> bytes:
        for p in self.root.rglob("*"):
            if p.is_file() and f"local-{p.stat().st_ino}" == f.file_id:
                return p.read_bytes()
        raise SourceError(f"Arquivo não encontrado ao ler: {f.name}")


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------
class DriveSource:
    """Lê apenas a árvore da pasta configurada (escopo drive.readonly)."""

    kind = "drive"
    LIST_FIELDS = (
        "nextPageToken, files(id, name, mimeType, modifiedTime, version, md5Checksum, "
        "webViewLink, size, parents, capabilities(canDownload))"
    )

    def __init__(self, credentials, root_folder_id: str):
        from googleapiclient.discovery import build

        self.root_id = root_folder_id
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    def _call(self, request, attempts: int = 4):
        """Executa uma chamada com atraso progressivo em erros temporários (429/5xx)."""
        from googleapiclient.errors import HttpError

        delay = 1.0
        for i in range(attempts):
            try:
                return request.execute()
            except HttpError as e:
                status = getattr(e.resp, "status", 0)
                temporary = status in (429, 500, 502, 503, 504) or (
                    status == 403 and "rateLimit" in str(e)
                )
                if temporary and i < attempts - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise SourceError(f"Drive API respondeu {status}: {e.reason if hasattr(e, 'reason') else e}",
                                  temporary=temporary) from e
            except OSError as e:  # rede
                if i < attempts - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise SourceError(f"Falha de rede ao acessar o Drive: {e}", temporary=True) from e

    def list_tree(self) -> list[RemoteFile]:
        out: list[RemoteFile] = []
        visited: set[str] = set()
        queue: list[tuple[str, str]] = [(self.root_id, "/")]
        while queue:
            folder_id, folder_path = queue.pop(0)
            if folder_id in visited:
                continue
            visited.add(folder_id)
            page_token = None
            while True:
                resp = self._call(
                    self.service.files().list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        spaces="drive",
                        pageSize=1000,
                        pageToken=page_token,
                        fields=self.LIST_FIELDS,
                    )
                )
                for item in resp.get("files", []):
                    if item["mimeType"] == FOLDER:
                        queue.append((item["id"], folder_path.rstrip("/") + "/" + item["name"]))
                        continue
                    out.append(
                        RemoteFile(
                            file_id=item["id"],
                            name=item["name"],
                            mime_type=item["mimeType"],
                            modified_at=item.get("modifiedTime"),
                            version=item.get("version"),
                            md5=item.get("md5Checksum"),
                            web_url=item.get("webViewLink"),
                            path=folder_path,
                            parent_id=folder_id,
                            can_download=item.get("capabilities", {}).get("canDownload", True),
                        )
                    )
                page_token = resp.get("nextPageToken")
                if not page_token:
                    break
        return out

    def fetch(self, f: RemoteFile) -> bytes:
        if not f.can_download:
            raise SourceError("A conta autorizada não tem permissão de download deste arquivo.")
        files = self.service.files()
        if f.mime_type in (GDOC, GSLIDES):   # Docs e Slides nativos: exportados como texto
            data = self._call(files.export(fileId=f.file_id, mimeType="text/plain"))
        elif f.mime_type == GSHEET:
            data = self._call(files.export(fileId=f.file_id, mimeType=XLSX))
        else:
            data = self._call(files.get_media(fileId=f.file_id))
        if isinstance(data, str):
            data = data.encode("utf-8")
        return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
