# SoundCloud → AIFF / WAV

<p align="center">
  <img src="logo-wordmark.svg" alt="SoundCloud → AIFF / WAV" width="520">
</p>

Small **local** tool to download SoundCloud tracks in high quality (16-bit PCM AIFF or WAV) with **embedded cover art** and **clean filenames**, for **personal music library** use.

A small web page opens at `http://localhost:8765`, you paste a SoundCloud link, pick AIFF or WAV, and the file lands in `~/Downloads/SoundCloud/`.

---

## 🚫 Usage policy

This tool is provided **strictly for personal and private use**: building a music library at home. You may **not** use the produced files:

- ❌ in **nightclubs, clubs, bars, festivals**,
- ❌ in **public DJ sets**, livestreams, radio shows,
- ❌ in any **public, paid or commercial broadcast**,
- ❌ to **redistribute** them (uploading, sharing, reselling).

For any public play, please buy from professional platforms (Beatport, Bandcamp, label promo pools, etc.) that pay the artists — that is the only correct way to support the scene.

---

## ⚠️ Strictly local — never host this on the internet

This project is designed to run **only on the user's own machine**, on `127.0.0.1`. It is **not** intended, **not** allowed and **not** built to be:

- deployed on a public server,
- hosted online (VPS, public container, SaaS platform, etc.),
- exposed through a tunnel (ngrok, Cloudflare Tunnel, etc.),
- offered as a service to third parties.

Why:

- **Legal / copyright**: downloading SoundCloud tracks may violate their Terms of Service and copyright depending on your jurisdiction. This tool is for **personal, private use** — only on tracks you have the right to download (your own uploads, freely-licensed content, or use that is permitted locally).
- **Security**: the local HTTP server is minimal, with no authentication or rate limiting. Exposing it to the internet would invite abuse instantly.

**If you fork this repo, you must keep this restriction.** Any public hosting or commercial use is explicitly not authorized.

---

## What it actually does

When preparing a DJ set you need files that are:

- **uncompressed** (AIFF / WAV) to preserve dynamics,
- **clearly named** (clean title, no "Première: " or other noise),
- **with embedded cover art** in the ID3 tag, so the track shows up correctly in your library software (Rekordbox, Serato, Traktor, Engine DJ…).

That's exactly what the tool does, in one step.

---

## Requirements

- macOS / Linux (tested on macOS)
- Python 3.9+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) and [`ffmpeg`](https://ffmpeg.org/) on `PATH`

On macOS:

```bash
brew install yt-dlp ffmpeg
```

---

## Run it

```bash
python3 app.py
```

Then open [http://localhost:8765](http://localhost:8765) in your browser.

Files are written to `~/Downloads/SoundCloud/`.

---

## How it works

1. **Preview**: `yt-dlp --no-download` fetches title, uploader and cover URL → shown in the UI.
2. **Download**: `yt-dlp -f bestaudio --write-thumbnail` downloads the best available audio + the cover as a JPG, into a temp directory.
3. **Convert**: `ffmpeg` re-encodes to 16-bit PCM (big-endian for AIFF, little-endian for WAV) and embeds the cover as `attached_pic` (ID3 APIC tag).
4. **Progress**: `yt-dlp` output is read line by line and streamed to the browser via Server-Sent Events.

All in ~270 lines of Python with no external dependencies beyond `yt-dlp` and `ffmpeg`.

---

## Layout

```
.
├── app.py             # HTTP server + yt-dlp / ffmpeg logic
├── index.html         # Web UI (vanilla, no framework)
├── logo.svg           # Square app icon
├── logo-wordmark.svg  # Horizontal logo with wordmark
└── README.md
```

---

## License

Code provided for personal and educational use. No warranty. Use it within applicable law and within the terms of service of the platforms involved.
