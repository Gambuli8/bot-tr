"""
Obtiene el GOOGLE_REFRESH_TOKEN para subir resúmenes a Drive. Se corre UNA vez,
en tu computadora (necesita navegador):

    python -m bot.google_auth --client-id XXX --client-secret YYY

Pasos previos (Google Cloud Console, gratis):
  1. Crear un proyecto → habilitar "Google Drive API".
  2. Pantalla de consentimiento OAuth: tipo "Externo", agregarte como usuario de prueba
     y publicarla ("En producción") para que el token no venza a los 7 días.
  3. Credenciales → Crear ID de cliente OAuth → "App de escritorio".

Imprime el refresh token: copialo vos al .env del VPS (GOOGLE_REFRESH_TOKEN).
"""

from __future__ import annotations

import argparse
import http.server
import secrets
import threading
import urllib.parse
import webbrowser

import requests

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.file"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    redirect = f"http://127.0.0.1:{args.port}/"
    state = secrets.token_urlsafe(16)
    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Listo. Podés cerrar esta pestaña y volver a la terminal.".encode())
            threading.Thread(target=server.shutdown, daemon=True).start()

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })
    print("Abriendo el navegador para autorizar Google Drive…\nSi no se abre, entrá a:\n" + url)
    webbrowser.open(url)
    server.serve_forever()

    if result.get("state") != state or "code" not in result:
        raise SystemExit(f"Autorización fallida: {result}")
    resp = requests.post(TOKEN_URL, data={
        "code": result["code"], "client_id": args.client_id, "client_secret": args.client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    token = resp.json().get("refresh_token")
    if not token:
        raise SystemExit("Google no devolvió refresh_token. Revocá el acceso de la app y reintentá.")
    print("\nGOOGLE_REFRESH_TOKEN=" + token)


if __name__ == "__main__":
    main()
