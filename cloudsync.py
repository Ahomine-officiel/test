#!/usr/bin/env python3
"""
CloudSync Server - Serveur cloud auto-hébergé pour le mod Minecraft CloudSync.

Modifié pour utiliser un TOKEN STATIQUE et tourner sur Hugging Face Spaces 24/7.
"""

import argparse
import json
import os
import re
import secrets
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# =============================================================================
#  TOKEN STATIQUE PAR DÉFAUT
# =============================================================================
# Remplace cette valeur par le token de ton choix :
DEFAULT_STATIC_TOKEN = "MON_SUPER_TOKEN_SECRET_123"


# =============================================================================
#  Configuration
# =============================================================================

class Config:
    """Configuration du serveur (parsée depuis argv)."""
    token: str = DEFAULT_STATIC_TOKEN
    host: str = "0.0.0.0"
    port: int = 8080
    data_dir: Path = Path("./data")
    ssl_cert: str = ""
    ssl_key: str = ""
    max_file_size: int = 2 * 1024 * 1024 * 1024  # 2 Go max par fichier


# =============================================================================
#  Utilitaires
# =============================================================================

SAFE_NAME_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


def sanitize_name(name: str) -> str:
    """Valide et nettoie un nom de fichier."""
    if not name or len(name) > 255:
        raise ValueError(f"Nom de fichier invalide: {name!r}")
    if not SAFE_NAME_RE.match(name):
        raise ValueError(f"Nom de fichier contient des caractères interdits: {name!r}")
    if name in ('.', '..', '.git', '.gitignore'):
        raise ValueError(f"Nom de fichier réservé: {name!r}")
    return name


def file_meta_path(name: str) -> Path:
    return Config.data_dir / f"{name}.meta.json"


def file_data_path(name: str) -> Path:
    return Config.data_dir / name


def write_meta(name: str, size: int):
    meta = {
        "name": name,
        "size": size,
        "modifiedTime": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = file_meta_path(name)
    tmp = meta_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=2))
    tmp.replace(meta_path)


def read_meta(name: str) -> dict:
    meta_path = file_meta_path(name)
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text())
    except Exception:
        return {}


# =============================================================================
#  Handler HTTP CloudSync
# =============================================================================

class CloudSyncHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{self.log_date_time_string()}] {self.address_string()} - {fmt % args}\n")

    def check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:].strip()
        return secrets.compare_digest(token, Config.token)

    def send_unauthorized(self):
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(json.dumps({"error": "unauthorized", "message": "Token manquant ou invalide"}).encode())

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, Content-Length")

    def send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: int, message: str):
        self.send_json(status, {"error": message})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/health":
            self.send_json(200, {"status": "ok", "version": "1.0.0"})
            return

        if not self.check_auth():
            self.send_unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/list":
            files = []
            for f in Config.data_dir.iterdir():
                if f.is_file() and not f.name.endswith(".meta.json") and not f.name.startswith("."):
                    meta = read_meta(f.name)
                    files.append({
                        "name": f.name,
                        "size": meta.get("size", f.stat().st_size),
                        "modifiedTime": meta.get("modifiedTime", datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat()),
                    })
            self.send_json(200, {"files": files})
            return

        if path.startswith("/api/files/"):
            rest = path[len("/api/files/"):]
            if rest.endswith("/content"):
                name = rest[:-len("/content")]
                self.handle_download(name)
            else:
                name = rest
                self.handle_get_meta(name)
            return

        if path == "/" or path == "":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<!DOCTYPE html><html><body><h1>CloudSync Server</h1><p>OK</p></body></html>")
            return

        self.send_error_json(404, f"Route inconnue: {path}")

    def handle_get_meta(self, name: str):
        try:
            name = sanitize_name(name)
        except ValueError as e:
            self.send_error_json(400, str(e))
            return
        data_path = file_data_path(name)
        if not data_path.exists():
            self.send_error_json(404, f"Fichier introuvable: {name}")
            return
        meta = read_meta(name)
        self.send_json(200, {
            "fileId": name,
            "name": name,
            "modifiedTime": meta.get("modifiedTime", datetime.fromtimestamp(data_path.stat().st_mtime, timezone.utc).isoformat()),
            "size": meta.get("size", data_path.stat().st_size),
            "md5Checksum": None,
        })

    def handle_download(self, name: str):
        try:
            name = sanitize_name(name)
        except ValueError as e:
            self.send_error_json(400, str(e))
            return
        data_path = file_data_path(name)
        if not data_path.exists():
            self.send_error_json(404, f"Fichier introuvable: {name}")
            return
        size = data_path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_cors_headers()
        self.end_headers()
        with open(data_path, "rb") as f:
            while True:
                chunk = f.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def do_PUT(self):
        if not self.check_auth():
            self.send_unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/files/") and path.endswith("/content"):
            name = path[len("/api/files/"):-len("/content")]
            self.handle_upload(name)
            return

        self.send_error_json(404, f"Route inconnue: {path}")

    def handle_upload(self, name: str):
        try:
            name = sanitize_name(name)
        except ValueError as e:
            self.send_error_json(400, str(e))
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > Config.max_file_size:
            self.send_error_json(413, f"Fichier trop volumineux: {content_length} > {Config.max_file_size}")
            return

        data_path = file_data_path(name)
        tmp_path = data_path.with_suffix(data_path.suffix + ".tmp")

        try:
            with open(tmp_path, "wb") as f:
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(64 * 1024, remaining)
                    chunk = self.rfile.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
            tmp_path.replace(data_path)
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            self.send_error_json(500, f"Erreur écriture: {e}")
            return

        write_meta(name, content_length)
        meta = read_meta(name)
        self.send_json(200, {
            "fileId": name,
            "name": name,
            "modifiedTime": meta.get("modifiedTime"),
            "size": content_length,
            "md5Checksum": None,
        })

    def do_DELETE(self):
        if not self.check_auth():
            self.send_unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/files/"):
            name = path[len("/api/files/"):]
            try:
                name = sanitize_name(name)
            except ValueError as e:
                self.send_error_json(400, str(e))
            return
            data_path = file_data_path(name)
            meta_path = file_meta_path(name)
            deleted = False
            if data_path.exists():
                data_path.unlink()
                deleted = True
            if meta_path.exists():
                meta_path.unlink()
            if not deleted:
                self.send_error_json(404, f"Fichier introuvable: {name}")
                return
            self.send_json(200, {"deleted": name})
            return

        self.send_error_json(404, f"Route inconnue: {path}")


class CloudSyncServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# =============================================================================
#  Serveur Web de garde pour Hugging Face (Port 7860)
# =============================================================================

class HFKeepAliveHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Désactive les logs du keep-alive pour garder la console propre

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = f"""
        <!DOCTYPE html>
        <html>
        <head><title>CloudSync Server</title></head>
        <body style="font-family: sans-serif; padding: 20px;">
            <h1>☁️ CloudSync Server est actif 24/7</h1>
            <p><strong>Port d'écoute API :</strong> {Config.port}</p>
            <p><strong>Token d'authentification :</strong> <code>{Config.token}</code></p>
        </body>
        </html>
        """
        self.wfile.write(html.encode('utf-8'))


def run_cloudsync():
    """Lance le serveur principal CloudSync."""
    Config.data_dir.mkdir(parents=True, exist_ok=True)
    server = CloudSyncServer((Config.host, Config.port), CloudSyncHandler)
    print("=" * 70)
    print("  ☁  CloudSync Server")
    print("=" * 70)
    print(f"  Port API : {Config.port}")
    print(f"  Stockage : {Config.data_dir}")
    print(f"  ★ Token Statique : {Config.token}")
    print("=" * 70)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="CloudSync Server")
    parser.add_argument("--token", "-t", default=DEFAULT_STATIC_TOKEN,
                        help=f"Token d'authentification (statique par défaut: {DEFAULT_STATIC_TOKEN})")
    parser.add_argument("--host", default="0.0.0.0", help="Host d'écoute")
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port d'écoute (défaut: 8080)")
    parser.add_argument("--data-dir", "-d", default="./data", help="Dossier de stockage")

    args, _ = parser.parse_known_args()

    Config.token = args.token
    Config.host = args.host
    Config.port = args.port
    Config.data_dir = Path(args.data_dir).resolve()

    # 1. Lancer CloudSync dans un Thread séparé en arrière-plan
    sync_thread = threading.Thread(target=run_cloudsync, daemon=True)
    sync_thread.start()

    # 2. Lancer un micro-serveur sur le port 7860 (port attendu par Hugging Face Spaces)
    hf_port = int(os.environ.get("PORT", 7860))
    hf_server = ThreadingHTTPServer(("0.0.0.0", hf_port), HFKeepAliveHandler)
    print(f"  [HF Keep-Alive] Serveur de garde actif sur le port {hf_port}")
    
    try:
        hf_server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt du serveur...")


if __name__ == "__main__":
    main()