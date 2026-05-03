#!/usr/bin/env python3
# =============================================================================
#  SoundCloud → AIFF/WAV — small local HTTP server
#  Author: hillzeealex  —  https://github.com/hillzeealex/soundcloud-dl
# -----------------------------------------------------------------------------
#  ⚠️  USAGE POLICY (please read before use)
#
#  This tool is provided for STRICTLY PERSONAL AND PRIVATE USE: building a
#  music library at home from tracks you are legally allowed to download.
#
#  YOU MUST NOT USE THE FILES PRODUCED BY THIS TOOL:
#     ✗ in nightclubs, clubs, bars, festivals,
#     ✗ in public DJ sets, livestreams, radio shows,
#     ✗ in any public, paid or commercial broadcast,
#     ✗ to redistribute them (uploads, sharing, reselling).
#
#  In addition, this tool MUST NEVER be hosted on the internet: it runs
#  only on 127.0.0.1, on the user's own machine.
#
#  For club / radio / streaming use, please buy from professional platforms
#  (Beatport, Bandcamp, label promo pools, etc.) that pay the artists.
# =============================================================================
"""
SoundCloud → AIFF/WAV — small local HTTP server.

How it works:
    1. A small web page (index.html) is served on http://localhost:8765.
    2. The user pastes a SoundCloud URL → a preview (title, artist, cover)
       is fetched via yt-dlp in "no-download" mode.
    3. On click, yt-dlp downloads the best available audio + the cover,
       then ffmpeg converts to 16-bit PCM AIFF or WAV with the cover
       embedded as an ID3 APIC tag.
    4. Progress is streamed back to the browser via Server-Sent Events.

See the banner above for the usage policy.
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
HOST = "127.0.0.1"  # localhost only — do not expose on the network
DOWNLOADS = Path.home() / "Downloads" / "SoundCloud"
DOWNLOADS.mkdir(parents=True, exist_ok=True)

INDEX_HTML = (Path(__file__).parent / "index.html").read_text()


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

def safe_name(s: str) -> str:
    """Strip characters that are not allowed in filenames."""
    return re.sub(r'[\\/:*?"<>|]+', "_", s).strip() or "track"


def strip_premiere_prefix(s: str) -> str:
    """
    Strip a leading "Première: " / "Prémière: " / "PREMIERE: " prefix that
    SoundCloud sometimes adds. Case-insensitive and accent-insensitive on
    both "e"s, so it matches: Premiere, Première, Prémière, PRÉMIÈRE, etc.
    """
    s = s.strip()
    cleaned = re.sub(r"^pr[ée]mi[èe]re\s*[:：]\s*", "", s, flags=re.IGNORECASE).strip()
    return cleaned or s


# ---------------------------------------------------------------------------
# Preview (before download)
# ---------------------------------------------------------------------------

def preview(url: str) -> dict:
    """Ask yt-dlp for metadata without downloading anything."""
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
# Download + conversion (streamed via SSE)
# ---------------------------------------------------------------------------

def stream_download(url: str, fmt: str):
    """
    Generator yielding (event_name, data_dict) tuples while downloading and
    converting. Possible events:
        - stage    : stage change (e.g. "Converting to AIFF…")
        - progress : yt-dlp percentage
        - log      : raw line (debug)
        - done     : success (final path, size, etc.)
        - error    : error message
    """
    if fmt not in ("aiff", "wav"):
        yield "error", {"error": "invalid format"}
        return

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        out_template = str(tmp / "%(title)s.%(ext)s")

        # 1. Download audio + thumbnail with yt-dlp
        yield "stage", {"label": "Downloading audio…"}
        proc = subprocess.Popen(
            [
                "yt-dlp",
                "--no-playlist",
                "--newline",                       # one line per progress update
                "-f", "bestaudio",                 # best available quality
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
            yield "error", {"error": "yt-dlp failed"}
            return

        # 2. Locate files produced by yt-dlp
        audio = next(
            (p for p in tmp.iterdir() if p.suffix not in (".jpg", ".png", ".webp")),
            None,
        )
        thumb = next((p for p in tmp.iterdir() if p.suffix == ".jpg"), None)
        if not audio:
            yield "error", {"error": "audio file not found"}
            return

        title = safe_name(strip_premiere_prefix(audio.stem))
        final = DOWNLOADS / f"{title}.{fmt}"

        # 3. ffmpeg conversion to AIFF/WAV with embedded cover art
        yield "stage", {"label": f"Converting to {fmt.upper()} + embedding cover…"}
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

        # AIFF = PCM big-endian, WAV = PCM little-endian (16-bit in both cases)
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
# HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    """Simple routing: GET / serves the page, POST /preview and POST /download."""

    # ----- helpers -----

    def _send_json(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, event: str, data: dict) -> bool:
        """Send an SSE event. Returns False if the client disconnected."""
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
        elif self.path == "/logo.svg":
            logo_path = Path(__file__).parent / "logo.svg"
            body = logo_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
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
                self._send_json(400, {"error": "invalid URL"})
                return

            if self.path == "/preview":
                self._send_json(200, {"ok": True, **preview(url)})

            elif self.path == "/download":
                fmt = payload.get("format", "aiff")
                # SSE headers
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                for event, data in stream_download(url, fmt):
                    if not self._send_sse(event, data):
                        return  # client disconnected

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
    """Allow handling a /preview while a /download is streaming."""
    daemon_threads = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not shutil.which("yt-dlp") or not shutil.which("ffmpeg"):
        raise SystemExit("yt-dlp and ffmpeg are required (brew install yt-dlp ffmpeg)")

    print("┌─────────────────────────────────────────────────────────────┐")
    print("│  SoundCloud → AIFF/WAV  —  personal use only                │")
    print("│  Not for clubs, radio, or any public broadcast.             │")
    print("└─────────────────────────────────────────────────────────────┘")
    print(f"→ http://localhost:{PORT}")
    print(f"→ Downloads folder: {DOWNLOADS}")
    ThreadingServer((HOST, PORT), Handler).serve_forever()
