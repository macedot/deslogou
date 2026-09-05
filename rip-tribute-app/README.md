# RIP Tribute

A shareable web service that turns a person's name into a memorial tribute
video. The flow is two numbered buttons:

1. **SEARCH PHOTO** — finds the person's photo on Wikipedia (pt, then en).
   After the search — success or not — a custom photo can be supplied via
   **URL or file upload**.
2. **RENDER** — renders the tribute (photo crossfade, optional caption
   "RIP: \<Name\> \<date\>" via a checkbox, sad Chaves theme over the
   pre-rendered template) and then **automatically merges it after the
   provided intro clip** with a configurable gap. Both the tribute and the
   final video are validated and returned.

Built with FastAPI + Pillow + ffmpeg.

## Repo layout

- `tribute-prerender.mp4` — the pre-rendered black+theme template. **Not part
  of the Docker image**; mounted into the container at runtime
  (`PRERENDER_PATH`).
- `intro.mp4` — the provided intro clip placed **before** the tribute: news
  clip with its original audio (faded out at the end) plus the WhatsApp voice
  note overlaid from 1s. Also mounted at runtime (`INTRO_PATH`).
- `rip-tribute-app/` — the service (built by `Dockerfile` and the GitHub
  Action, which publishes the image to `ghcr.io/macedot/deslogou`).
- `docker-compose.yml` — wires image + template + intro + video storage.

## Run locally (Docker Compose)

```bash
docker compose up --build
```

Open http://localhost:8000 — type a name, confirm the photo, hit Render.

## Run locally without Docker

Requires Python 3.10+ and ffmpeg/ffprobe on PATH (`brew install ffmpeg`).
The template/intro paths must be provided:

```bash
cd rip-tribute-app
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PRERENDER_PATH=../tribute-prerender.mp4 INTRO_PATH=../intro.mp4 \
  .venv/bin/uvicorn app.main:app --port 8000
```

## API

| Endpoint | Body | Result |
|---|---|---|
| `POST /api/lookup` | JSON `{"name": "Chico Anysio"}` | `{name, photo_url, page_url, lang}` |
| `POST /api/render` | multipart: `name`, `date?` (DD/MM/YYYY), `caption?` (bool, default true — false skips the RIP text), `photo_url?`, `fade?` (s), `photo?` (image file) | `{tribute: {...}, final: {...}, validated: true}` — each of `tribute`/`final` carries `video_url`, `file`, `duration`, `audio_mean_db`, `frame_luma` |
| `GET /video/{id}.mp4` | — | tribute (`{id}`) or final (`final_{id}`) mp4 |
| `GET /api/version` | — | `{version, template_ok, intro_ok}` — deployment identity |

The photo source order in `/api/render` is: **uploaded file → photo_url →
Wikipedia lookup**. Any public http(s) URL is accepted (private/internal
addresses are rejected to prevent SSRF); Wikipedia photos are served from
`upload.wikimedia.org`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `PRERENDER_PATH` | `/data/prerender.mp4` | Pre-rendered template location. Renders fail without it. |
| `INTRO_PATH` | `/data/intro.mp4` | Provided intro clip placed before the tribute. |
| `MERGE_GAP` | `1` | Seconds of silent black between intro and tribute (config, not UI). |
| `GENERATED_DIR` | `/srv/generated` (in image: app-adjacent) | Where rendered videos are written. Mount a volume for persistence. |

## Validation guarantees

Both stages are verified; a file that fails any check is deleted, never
served, and the API returns a 500 explaining why:

- **Template (before rendering)** — `PRERENDER_PATH` must exist and be a
  1920x1080 mp4 with both video and audio streams.
- **Tribute output** — both streams, duration matches the template (±0.5s),
  audible theme music (> −60 dB post-dissolve), non-black picture.
- **Intro (before merge)** — has a video stream, ≤ 180s.
- **Final output** — both streams, duration matches intro + gap + tribute
  (±1s), theme audible and picture non-black inside the tribute region.

## Deploy notes

- Any Docker host: `docker compose up -d`, or run
  `ghcr.io/macedot/deslogou:latest` with the mounts above.
- The GHCR image is public; the footer in the UI shows the build version so
  you can confirm the proper image is deployed.
- Renders are ephemeral unless `/srv/generated` is a volume.
- No auth or rate limiting — fine for friends, add a token or `slowapi`
  before exposing publicly.
