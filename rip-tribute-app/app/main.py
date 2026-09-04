"""RIP Tribute web service: name in, memorial video out."""

import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import render, wiki

BASE_DIR = Path(__file__).resolve().parent.parent
GENERATED_DIR = Path(os.environ.get("GENERATED_DIR", BASE_DIR / "generated"))
GENERATED_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="RIP Tribute", description="Memorial tribute videos from a person's name")


class LookupRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class RenderRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    date: str | None = Field(default=None, description="DD/MM/YYYY, defaults to today")
    photo_url: str | None = Field(default=None, description="Wikimedia photo URL; looked up on Wikipedia if omitted")
    fade: float = Field(default=1.5, ge=0.2, le=5)


def _today() -> str:
    try:
        return datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y")
    except ZoneInfoNotFoundError:
        return datetime.utcnow().strftime("%d/%m/%Y")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.post("/api/lookup")
def lookup(req: LookupRequest) -> dict:
    try:
        return wiki.find_photo(req.name)
    except wiki.PhotoNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Wikipedia lookup failed: {exc}") from exc


@app.post("/api/render")
def render_video(req: RenderRequest) -> dict:
    if req.date and not re.fullmatch(r"\d{2}/\d{2}/\d{4}", req.date):
        raise HTTPException(status_code=422, detail="date must be DD/MM/YYYY")

    photo_url = req.photo_url
    if not photo_url:
        try:
            photo_url = wiki.find_photo(req.name)["photo_url"]
        except wiki.PhotoNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Wikipedia lookup failed: {exc}") from exc

    try:
        expected_duration = render.validate_template(render.PRERENDER)
    except render.TemplateError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    with tempfile.TemporaryDirectory() as tmp:
        try:
            photo = render.download_photo(photo_url, tmp)
        except render.PhotoError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        out = GENERATED_DIR / f"{uuid.uuid4().hex}.mp4"
        try:
            render.make_tribute(req.name.strip(), photo, out, date=req.date or _today(), fade=req.fade)
            checks = render.validate_output(out, expected_duration)
        except (render.TemplateError, render.ValidationError) as exc:
            out.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        except Exception as exc:
            out.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"Render failed: {exc}") from exc

    return {"video_url": f"/video/{out.stem}.mp4", "file": out.name, "validated": True, **checks}


@app.get("/video/{vid}.mp4")
def video(vid: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{32}", vid):
        raise HTTPException(status_code=404, detail="Not found")
    path = GENERATED_DIR / f"{vid}.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Video not found (renders may be cleaned up on redeploy)")
    return FileResponse(path, media_type="video/mp4", filename="rip-tribute.mp4")
