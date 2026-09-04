# RIP Tribute

A shareable web service that turns a person's name into a memorial tribute
video: their Wikipedia photo dissolves in, centered on a black screen, with
the caption "RIP: \<Name\> \<date\>" and the Chaves sad theme playing.

Built with FastAPI + Pillow + ffmpeg. Renders a 10.55s 1080p mp4 per request.

## Run locally

Requires Python 3.11+ and ffmpeg on PATH (`brew install ffmpeg`).

```bash
cd rip-tribute-app
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000 — type a name, confirm the photo found on
Wikipedia, generate, play, download.

## API

| Endpoint | Body | Result |
|---|---|---|
| `POST /api/lookup` | `{"name": "Chico Anysio"}` | `{name, photo_url, page_url, lang}` |
| `POST /api/render` | `{"name": "...", "date": "DD/MM/YYYY?", "photo_url": "...?", "fade": 1.5?}` | `{video_url, file}` |
| `GET /video/{id}.mp4` | — | the rendered mp4 |

`photo_url` is optional; when omitted the server looks the photo up on
Wikipedia (pt first, then en). When provided it must be an
`upload.wikimedia.org` URL — this keeps the server from being tricked into
fetching internal addresses (SSRF).

## Deploy

### Docker Compose (any machine with Docker)

From the repo root — builds the image locally (or pulls
`ghcr.io/macedot/deslogou:latest` once published) and keeps rendered videos in
a named volume:

```bash
docker compose up --build
```

The service is then on http://localhost:8000.

### Fly.io (config included)

```bash
brew install flyctl
fly auth login
cd rip-tribute-app
fly launch --no-deploy   # confirms app name/region from fly.toml
fly deploy
```

`fly.toml` uses the `gru` (São Paulo) region and scales to zero when idle.

### Railway / Render / any Docker host

No special config needed — they detect the Dockerfile and start
`uvicorn` on `$PORT`... note the Dockerfile hardcodes port 8000; on platforms
that inject `$PORT`, change the CMD to
`uvicorn app.main:app --host 0.0.0.0 --port ${PORT}` or use a Dockerfile
env override.

## Notes & follow-ups

- **Renders are ephemeral** on Fly: files in `generated/` disappear when a
  machine restarts. The UI plays/downloads immediately, so this is usually
  fine; add a Fly volume (`fly volumes create`) mounted at `/srv/generated`
  if you want persistence.
- **No auth or rate limiting yet** — fine for friends, not for the open
  internet. Add a shared token or `slowapi` rate limiting before sharing
  publicly.
- **Photo source is Wikipedia-only** for now; the lookup raises a clean 404
  when no photo exists. A fallback image search (e.g. an image-search API)
  would slot into `app/wiki.py`.
- Date defaults to today in America/Sao_Paulo.
