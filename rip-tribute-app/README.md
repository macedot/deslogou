# RIP Tribute

A shareable web service that turns a person's name into a memorial tribute
video in **two phases**: phase 1 renders the tribute (their Wikipedia photo
dissolving in over the black+theme pre-render, captioned "RIP: \<Name\>
\<date\>"); you validate it; phase 2 merges it after an intro clip you upload
into the final video.

Built with FastAPI + Pillow + ffmpeg.

## Repo layout

- `tribute-prerender.mp4` — the pre-rendered black+theme template. **Not part
  of the Docker image**; mounted into the container at runtime
  (`PRERENDER_PATH`).
- `intro.mp4` — the provided intro clip: news-clip visuals with the WhatsApp
  voice note as the only audio (1s in, black tail while it finishes). Also
  not in the image; mounted at runtime (`INTRO_PATH`).
- `rip-tribute-app/` — the service (built by `Dockerfile` and the GitHub
  Action, which publishes the image to `ghcr.io/macedot/deslogou`).
- `docker-compose.yml` — wires image + template + intro + video storage.

## Run locally (Docker Compose)

```bash
docker compose up --build
```

Open http://localhost:8000 — type a name, confirm the photo found on
Wikipedia, generate, play, download.

## Run locally without Docker

Requires Python 3.10+ and ffmpeg/ffprobe on PATH (`brew install ffmpeg`).
The template path must be provided:

```bash
cd rip-tribute-app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PRERENDER_PATH=../tribute-prerender.mp4 .venv/bin/uvicorn app.main:app --port 8000
```

## API

| Endpoint | Body | Result |
|---|---|---|
| `POST /api/lookup` | `{"name": "Chico Anysio"}` | `{name, photo_url, page_url, lang}` |
| `POST /api/render` *(phase 1)* | `{"name": "...", "date": "DD/MM/YYYY?", "photo_url": "...?", "fade": 1.5?}` | `{video_url, file, duration, audio_mean_db, frame_luma, validated: true}` |
| `POST /api/merge` *(phase 2)* | multipart: `intro` (optional video file), `tribute_id` (from phase 1), `gap` (seconds, 0–10, default 1) | `{video_url, file, duration, audio_mean_db, frame_luma, validated: true}` |
| `GET /video/{id}.mp4` | — | tribute (`{id}`) or final (`final_{id}`) mp4 |

Phase 2 appends the tribute after an intro clip. When `intro` is omitted, the
provided clip (`INTRO_PATH`) is used; an uploaded clip overrides it. The intro
is normalized to 1920x1080 (resolution, fps, codec — audio optional, silence
is synthesized if missing), then intro + black gap + tribute are concatenated
and validated.

`photo_url` is optional; when omitted the server looks the photo up on
Wikipedia (pt first, then en). When provided it must be an
`upload.wikimedia.org` URL — this keeps the server from being tricked into
fetching internal addresses (SSRF).

### Validation guarantees

Both phases are verified twice; a file that fails any check is deleted, never
served, and the API returns a 500 explaining why:

- **Template (before rendering)** — `PRERENDER_PATH` must exist and be a
  1920x1080 mp4 containing *both* a video and an audio stream.
- **Tribute output (after phase 1)** — the finished file must have both
  streams, match the template's duration (±0.5s), have audible theme music
  (mean volume above −60 dB in the post-dissolve window), and a non-black
  picture (the photo overlay actually happened).
- **Intro (before merge)** — must contain a video stream and be ≤ 180s /
  200MB.
- **Final output (after phase 2)** — both streams present, duration matches
  intro + gap + tribute (±1s), the theme is audible and the picture non-black
  inside the tribute region.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PRERENDER_PATH` | `/data/prerender.mp4` | Where the pre-rendered template lives. **Must point to `tribute-prerender.mp4`** — renders fail otherwise. |
| `INTRO_PATH` | `/data/intro.mp4` | The provided intro clip used by MERGE when no clip is uploaded. |
| `GENERATED_DIR` | `/srv/generated` (in image: app-adjacent) | Where rendered videos are written. Mount a volume here for persistence. |

## Deploy notes

- Any Docker host works: `docker compose up -d`, or run
  `ghcr.io/macedot/deslogou:latest` directly with the two mounts above.
- The GHCR image is private (private repo): `docker login ghcr.io` with a
  `read:packages` token to pull it, or build on the host.
- Renders are ephemeral unless `/srv/generated` is a volume.
- No auth or rate limiting — fine for friends, add a token or `slowapi`
  before exposing publicly.
