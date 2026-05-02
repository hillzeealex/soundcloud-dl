#!/usr/bin/env python3
"""
SoundCloud → AIFF/WAV — petit serveur HTTP local.

Fonctionnement général :
    1. On expose une page web (index.html) sur http://localhost:8765.
    2. L'utilisateur colle une URL SoundCloud → un aperçu (titre, artiste,
       pochette) est récupéré via yt-dlp en mode "no-download".
    3. Au clic sur "Télécharger", on lance yt-dlp pour récupérer le meilleur
       audio + la pochette, puis ffmpeg convertit en AIFF ou WAV PCM 16 bits
       avec la pochette intégrée comme tag ID3 APIC.
    4. La progression est renvoyée en direct au navigateur via Server-Sent
       Events (SSE).

⚠️  Cet outil est conçu pour un usage strictement local. Il ne doit pas être
    exposé sur Internet (voir README).
"""

import json
import re
import shutil
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = 8765
HOST = "127.0.0.1"  # localhost uniquement — ne pas exposer sur le réseau
DOWNLOADS = Path.home() / "Downloads" / "SoundCloud"
DOWNLOADS.mkdir(parents=True, exist_ok=True)

INDEX_HTML = (Path(__file__).parent / "index.html").read_text()


# ---------------------------------------------------------------------------
# Utilitaires sur les noms de fichiers
# ---------------------------------------------------------------------------

def safe_name(s: str) -> str:
    """Retire les caractères interdits dans un nom de fichier."""
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "track"


def strip_premiere_prefix(s: str) -> str:
    """Retire un éventuel préfixe « Première : » présent sur SoundCloud."""
    s = s.strip()
    cleaned = re.sub(r"^premi[èe]re\s*[:：]\s*", "", s, flags=re.IGNORECASE).strip()
    return cleaned or s


# ---------------------------------------------------------------------------
# Aperçu (avant téléchargement)
# ---------------------------------------------------------------------------

def preview(url: str) -> dict:
    """Demande à yt-dlp les métadonnées sans rien télécharger."""
    result = subprocess.run(
        [
            "yt-dlp",
            "--no-playlist",
            "--no-download",
            "--print", "%(title)s\t%(uploader)s\t%(thumbnail)s",
            url,
        ],
        check=True, capture_output=True, text=True, timeout=20,
    )
    title, uploader, thumbnail = (result.stdout.strip().split("\t") + ["", "", ""])[:3]
    return {
        "title": strip_premiere_prefix(title),
        "uploader": uploader,
        "thumbnail": thumbnail,
    }


# ---------------------------------------------------------------------------
# Téléchargement + conversion (en streaming SSE)
# ---------------------------------------------------------------------------

def stream_download(url: str, fmt: str):
    """
    Générateur qui yield des tuples (event_name, data_dict) au fil du
    téléchargement et de la conversion. Les événements possibles :
        - stage    : changement d'étape (ex. "Conversion en AIFF…")
        - progress : pourcentage de yt-dlp
        - log      : ligne brute (debug)
        - done     : succès (chemin final, taille, etc.)
        - error    : message d'erreur
    """
    if fmt not in ("aiff", "wav"):
        yield "error", {"error": "format invalide"}
        return

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        out_template = str(tmp / "%(title)s.%(ext)s")

        # 1. Téléchargement audio + miniature avec yt-dlp
        yield "stage", {"label": "Téléchargement audio…"}
        proc = subprocess.Popen(
            [
                "yt-dlp",
                "--no-playlist",
                "--newline",                       # 1 ligne par mise à jour
                "-f", "bestaudio",                 # meilleure qualité dispo
                "--write-thumbnail",
                "--convert-thumbnails", "jpg",
                "-o", out_template,
                url,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        percent_re = re.compile(r"(\d+\.\d+)%")
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            match = percent_re.search(line)
            if match:
                yield "progress", {"percent": float(match.group(1))}
            yield "log", {"line": line[:200]}
        proc.wait()
        if proc.returncode != 0:
            yield "error", {"error": "yt-dlp a échoué"}
            return

        # 2. On localise les fichiers produits par yt-dlp
        audio = next(
            (p for p in tmp.iterdir() if p.suffix not in (".jpg", ".png", ".webp")),
            None,
        )
        thumb = next((p for p in tmp.iterdir() if p.suffix == ".jpg"), None)
        if not audio:
            yield "error", {"error": "audio introuvable"}
            return

        title = safe_name(strip_premiere_prefix(audio.stem))
        final = DOWNLOADS / f"{title}.{fmt}"

        # 3. Conversion ffmpeg vers AIFF/WAV avec pochette intégrée
        yield "stage", {"label": f"Conversion en {fmt.upper()} + intégration pochette…"}
        cmd = ["ffmpeg", "-y", "-i", str(audio)]
        if thumb:
            cmd += [
                "-i", str(thumb),
                "-map", "0:a", "-map", "1:v",
                "-c:v", "mjpeg",
                "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover",
                "-metadata:s:v", "comment=Cover (front)",
            ]
        else:
            cmd += ["-map", "0:a"]

        # AIFF = PCM big-endian, WAV = PCM little-endian (16 bits dans les deux cas)
        codec = "pcm_s16be" if fmt == "aiff" else "pcm_s16le"
        cmd += ["-c:a", codec, "-write_id3v2", "1", str(final)]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            yield "error", {"error": f"ffmpeg: {result.stderr.strip()[-200:]}"}
            return

        yield "done", {
            "path": str(final),
            "size": final.stat().st_size,
            "title": title,
            "filename": final.name,
        }


# ---------------------------------------------------------------------------
# Serveur HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """Routage simple : GET / sert la page, POST /preview et POST /download."""

    # ----- helpers -----

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, event: str, data: dict) -> bool:
        """Envoie un évènement SSE. Retourne False si le client a coupé."""
        chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    # ----- routes -----

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            payload = self._read_json()
            url = payload.get("url", "").strip()
            if not url.startswith("http"):
                self._send_json(400, {"error": "URL invalide"})
                return

            if self.path == "/preview":
                self._send_json(200, {"ok": True, **preview(url)})

            elif self.path == "/download":
                fmt = payload.get("format", "aiff")
                # Headers SSE
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                for event, data in stream_download(url, fmt):
                    if not self._send_sse(event, data):
                        return  # client déconnecté

            else:
                self.send_error(404)

        except subprocess.CalledProcessError as e:
            stderr = (e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr) or str(e)
            self._send_json(500, {"error": f"yt-dlp/ffmpeg: {stderr.strip()[-300:]}"})
        except Exception as e:
            self._send_json(500, {"error": str(e)})

    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")


class ThreadingServer(ThreadingMixIn, HTTPServer):
    """Permet de répondre à un /preview pendant qu'un /download est en cours."""
    daemon_threads = True


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not shutil.which("yt-dlp") or not shutil.which("ffmpeg"):
        raise SystemExit("yt-dlp et ffmpeg sont requis (brew install yt-dlp ffmpeg)")

    print(f"→ http://localhost:{PORT}")
    print(f"→ Téléchargements dans : {DOWNLOADS}")
    ThreadingServer((HOST, PORT), Handler).serve_forever()
