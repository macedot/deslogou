"""RIP Tribute web service: name in, final video out.

Flow: 1) SEARCH finds a photo on Wikipedia; 2) RENDER renders the tribute
(photo + caption + theme over the pre-rendered template) and automatically
merges it after the provided intro clip. The intro/gap are deployment
configuration, not user input.
"""

import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import render, wiki

BASE_DIR = Path(__file__).resolve().parent.parent
GENERATED_DIR = Path(os.environ.get("GENERATED_DIR", BASE_DIR / "generated"))
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
APP_VERSION = os.environ.get("APP_VERSION", "dev")
MERGE_GAP = float(os.environ.get("MERGE_GAP", "1.0"))

app = FastAPI(title="RIP Tribute", description="Memorial tribute videos from a person's name")


class LookupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)


def _today() -> str:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y")
    except ZoneInfoNotFoundError:
        return datetime.utcnow().strftime("%d/%m/%Y")


def _video_response(name: str) -> FileResponse:
    match = re.fullmatch(r"(final_)?([0-9a-f]{32})", name)
    if not match:
        raise HTTPException(status_code=404, detail="Not found")
    path = GENERATED_DIR / f"{name}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found (renders may be cleaned up on redeploy)")
    filename = "rip-tribute-final.mp4" if match.group(1) else "rip-tribute.mp4"
    return FileResponse(path, media_type="video/mp4", filename=filename)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/version")
def version() -> dict:
    """Deployment identity: which build is running and whether the mounted
    template/intro files are in place — used to confirm the right image
    was pushed and deployed."""
    return {
        "version": APP_VERSION,
        "template_ok": render.PRERENDER.is_file(),
        "intro_ok": render.INTRO_PATH.is_file(),
    }


@app.post("/api/lookup")
def lookup(req: LookupRequest) -> dict:
    try:
        return wiki.find_photo(req.name)
    except wiki.PhotoNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Wikipedia lookup failed: {exc}") from exc


@app.post("/api/render")
async def render_video(
    name: str = Form(...),
    date: str | None = Form(None),
    photo_url: str | None = Form(None),
    fade: float = Form(1.5),
    photo: UploadFile | None = File(None),
) -> dict:
    """Render the tribute and merge it after the provided intro.

    Photo source order: uploaded file, then photo_url, then Wikipedia lookup.
    """
    name = name.strip()
    if not 2 <= len(name) <= 120:
        raise HTTPException(status_code=422, detail="name must be 2-120 characters")
    if date and not re.fullmatch(r"\d{2}/\d{2}/\d{4}", date):
        raise HTTPException(status_code=422, detail="date must be DD/MM/YYYY")

    try:
        expected_duration = render.validate_template(render.PRERENDER)
    except render.TemplateError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory() as tmp:
        photo_path: Path | None = None
        if photo is not None and photo.filename:
            photo_path = Path(tmp) / "photo"
            size = 0
            with open(photo_path, "wb") as fh:
                while chunk := await photo.read(1 << 20):
                    size += len(chunk)
                    if size > render.MAX_DOWNLOAD_BYTES:
                        raise HTTPException(status_code=413, detail="Photo exceeds 25MB.")
                    fh.write(chunk)
            try:
                render.validate_photo_file(photo_path)
            except render.PhotoError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        elif photo_url:
            try:
                photo_path = render.download_photo(photo_url, tmp)
            except render.PhotoError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        else:
            try:
                found = wiki.find_photo(name)
            except wiki.PhotoNotFound as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail=f"Wikipedia lookup failed: {exc}") from exc
            try:
                photo_path = render.download_photo(found["photo_url"], tmp)
            except render.PhotoError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        tribute = GENERATED_DIR / f"{uuid.uuid4().hex}.mp4"
        final = GENERATED_DIR / f"final_{uuid.uuid4().hex}.mp4"
        try:
            render.make_tribute(name, photo_path, tribute, date=date or _today(), fade=fade)
            t_checks = render.validate_output(tribute, expected_duration)
            total = render.merge_with_intro(render.INTRO_PATH, tribute, final, gap=MERGE_GAP)
            f_checks = render.validate_merged(
                final,
                expected_duration=total,
                tribute_starts_at=total - t_checks["duration"],
            )
        except (render.TemplateError, render.ValidationError, render.IntroError) as exc:
            tribute.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            tribute.unlink(missing_ok=True)
            final.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Render failed: {exc}") from exc

    return {
        "tribute": {"video_url": f"/video/{tribute.stem}.mp4", "file": tribute.name, **t_checks},
        "final": {"video_url": f"/video/{final.stem}.mp4", "file": final.name, **f_checks},
        "validated": True,
    }


@app.get("/video/{name}.mp4")
def video(name: str) -> FileResponse:
    return _video_response(name)
