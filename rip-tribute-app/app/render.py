"""Tribute video rendering: photo + caption -> 10.55s mp4 over the black+theme template.

Requires ffmpeg/ffprobe on PATH and Pillow installed.
"""

import datetime
import json
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ASSETS = Path(__file__).resolve().parent / "assets"
PRERENDER = ASSETS / "prerender.mp4"
FRAME_W, FRAME_H = 1920, 1080
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_PHOTO_HOSTS = ("upload.wikimedia.org",)  # keep SSRF out: only Wikimedia sources
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


class PhotoError(Exception):
    pass


def download_photo(url: str, dest_dir: str) -> Path:
    """Download a photo from an allowed host and sanity-check it with Pillow."""
    from urllib.parse import urlparse

    host = urlparse(url).hostname or ""
    if host not in ALLOWED_PHOTO_HOSTS:
        raise PhotoError(f"photo_url must be a {ALLOWED_PHOTO_HOSTS[0]} URL")
    req = urllib.request.Request(url, headers={"User-Agent": "rip-tribute-app/1.0"})
    dest = Path(dest_dir) / "photo"
    try:
        with urllib.request.urlopen(req, timeout=20) as resp, open(dest, "wb") as fh:
            received = 0
            while chunk := resp.read(1 << 16):
                received += len(chunk)
                if received > MAX_DOWNLOAD_BYTES:
                    raise PhotoError("Photo is larger than 25MB")
                fh.write(chunk)
    except (urllib.error.URLError, OSError) as exc:
        raise PhotoError(f"Could not download photo: {exc}") from exc

    try:
        with Image.open(dest) as probe:
            probe.verify()
        with Image.open(dest) as probe:
            if probe.format not in ("JPEG", "PNG", "WEBP"):
                raise PhotoError(f"Unsupported photo format: {probe.format}")
    except PhotoError:
        raise
    except Exception as exc:
        raise PhotoError(f"Downloaded file is not a usable image: {exc}") from exc
    return dest


def _find_font() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("No usable TTF font found for the caption")


def _annotate(image_path: Path, caption: str) -> Path:
    """Scale photo to fit 1920x1080, burn the caption onto its bottom edge."""
    img = Image.open(image_path)
    img = ImageOps.exif_transpose(img).convert("RGB")
    scale = min(FRAME_W / img.width, FRAME_H / img.height)
    w = round(img.width * scale / 2) * 2
    h = round(img.height * scale / 2) * 2
    img = img.resize((w, h), Image.LANCZOS)
    draw = ImageDraw.Draw(img, "RGBA")

    font_path = _find_font()
    size, pad, margin = 48, 18, 60
    font = ImageFont.truetype(font_path, size)
    max_text_w = w - 2 * pad - 40
    while size > 20:
        bbox = draw.textbbox((0, 0), caption, font=font)
        if bbox[2] - bbox[0] <= max_text_w:
            break
        size -= 2
        font = ImageFont.truetype(font_path, size)

    bbox = draw.textbbox((0, 0), caption, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (w - tw) // 2
    y = h - th - margin
    draw.rectangle([x - pad, y - pad, x + tw + pad, y + th + pad], fill=(0, 0, 0, 140))
    draw.text((x, y), caption, font=font, fill=(255, 255, 255, 255))

    tmp = Path(tempfile.mkstemp(suffix=".png")[1])
    img.convert("RGBA").save(tmp)
    return tmp


def _probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def make_tribute(name: str, image: Path, out: Path, date: str | None = None,
                 fade: float = 1.5, prerender: Path = PRERENDER) -> Path:
    if not 0.2 <= fade <= 5:
        raise ValueError("fade must be between 0.2 and 5 seconds")
    date = date or datetime.datetime.now().strftime("%d/%m/%Y")
    caption = f"RIP: {name} {date}"
    annotated = _annotate(Path(image), caption)

    duration = _probe_duration(prerender)
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(prerender),
            "-loop", "1", "-t", f"{duration:.3f}", "-i", str(annotated),
            "-filter_complex",
            f"[1:v]scale=iw:ih,fade=t=in:st=0:d={fade}:alpha=1[isc];"
            "[0:v][isc]overlay=x=(W-w)/2:y=(H-h)/2[v]",
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(out),
        ],
        check=True,
    )
    annotated.unlink(missing_ok=True)
    return out
