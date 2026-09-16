"""
Subida de resúmenes a Google Drive (API REST, sin librerías de Google).

Autenticación: OAuth de "app de escritorio" con refresh token del usuario
(se obtiene una sola vez con `python -m bot.google_auth`). Scope `drive.file`:
el bot sólo ve y toca los archivos/carpetas que él mismo crea.

Los resúmenes se suben como Google Docs dentro de:
  Bot Trading BingX/Semanales  y  Bot Trading BingX/Mensuales
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Optional

import requests

from bot.store import Store

TOKEN_URL = "https://oauth2.googleapis.com/token"
FILES_URL = "https://www.googleapis.com/drive/v3/files"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_FOLDER_NAME = "Bot Trading BingX"


class DriveUploader:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 store: Store, root_folder_id: str = ""):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.store = store
        self.root_folder_id = root_folder_id
        self._token: Optional[str] = None
        self._token_exp = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        resp = requests.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _create_folder(self, name: str, parent: Optional[str]) -> str:
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            body["parents"] = [parent]
        resp = requests.post(FILES_URL, headers=self._headers(), json=body, params={"fields": "id"}, timeout=15)
        resp.raise_for_status()
        return resp.json()["id"]

    def _folder(self, key: str, name: str, parent: Optional[str]) -> str:
        folders = self.store.state.setdefault("drive_folders", {})
        if key not in folders:
            folders[key] = self._create_folder(name, parent)
            self.store.save()
        return folders[key]

    def upload_html_as_doc(self, name: str, html: str) -> str:
        root = self.root_folder_id or self._folder("root", ROOT_FOLDER_NAME, None)
        sub = "Mensuales" if "mensual" in name.lower() else "Semanales"
        folder = self._folder(sub.lower(), sub, root)

        boundary = uuid.uuid4().hex
        metadata = {"name": name, "mimeType": "application/vnd.google-apps.document", "parents": [folder]}
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(metadata)}\r\n"
            f"--{boundary}\r\nContent-Type: text/html; charset=UTF-8\r\n\r\n"
            f"{html}\r\n--{boundary}--"
        ).encode("utf-8")
        resp = requests.post(
            UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,webViewLink"},
            headers={**self._headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
            data=body, timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("webViewLink", "")
