"""Tribute video rendering: photo + caption -> 10.55s mp4 over the black+theme template.

Requires ffmpeg/ffprobe on PATH and Pillow installed. The pre-rendered
black+theme template is NOT bundled with the app: point PRERENDER_PATH at it.

Every render is validated twice: the template must be a real 1920x1080 video
with an audible audio track before rendering starts, and the rendered file is
checked afterwards (duration matches the template, theme actually audible,
picture not black) so a broken merge can never be served.
"""

import datetime
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageStat

PRERENDER = Path(os.environ.get("PRERENDER_PATH", "/data/prerender.mp4"))
FRAME_W, FRAME_H = 1920, 1080
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_PHOTO_HOSTS = ("upload.wikimedia.org",)  # keep SSRF out: only Wikimedia sources
FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]
SILENCE_FLOOR_DB = -60  # theme music sits around -15 dB; silence is ~-91 dB
DURATION_TOLERANCE_S = 0.5


class TemplateError(Exception):
    """The pre-rendered template is missing or not a valid black+theme base."""


class ValidationError(Exception):
    """The rendered video does not actually contain the merged template."""


class PhotoError(Exception):
    pass


class IntroError(Exception):
    """The uploaded intro video is missing or not usable."""


INTRO_MAX_DURATION_S = 180.0
INTRO_MAX_BYTES = 200 * 1024 * 1024


def _probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def _probe_duration(path: Path) -> float:
    return float(_probe(path)["format"]["duration"])


def _find_font() -> str:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return path
    raise RuntimeError("No usable TTF font found for the caption")


def validate_template(path: Path) -> float:
    """Verify the template is a usable black+theme base; return its duration."""
    path = Path(path)
    if not path.is_file():
        raise TemplateError(
            f"Pre-rendered template not found at {path}. "
            "Set PRERENDER_PATH to the tribute-prerender.mp4 location."
        )
    info = _probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None or audio is None:
        missing = "video" if video is None else "audio"
        raise TemplateError(
            f"Template at {path} has no {missing} stream. It must be "
            "tribute-prerender.mp4 (1080p black video with the theme music)."
        )
    if (video.get("width"), video.get("height")) != (FRAME_W, FRAME_H):
        raise TemplateError(
            f"Template is {video.get('width')}x{video.get('height')}, "
            f"but renders require {FRAME_W}x{FRAME_H}."
        )
    duration = float(info.get("format", {}).get("duration") or 0)
    if duration < 1:
        raise TemplateError(f"Template duration looks wrong ({duration:.3f}s).")
    return duration


def _mean_volume_db(path: Path, start: float, span: float) -> float:
    out = subprocess.run(
        ["ffmpeg", "-v", "info", "-ss", f"{start:.2f}", "-t", f"{span:.2f}",
         "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        check=True, capture_output=True, text=True,
    )
    match = re.search(r"mean_volume: (-?[\d.]+) dB", out.stderr)
    if not match:
        raise ValidationError("Could not measure audio level of rendered video.")
    return float(match.group(1))


def _frame_luma(path: Path, at: float) -> float:
    png = Path(tempfile.mkstemp(suffix=".png")[1])
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(path),
             "-frames:v", "1", str(png)],
            check=True,
        )
        with Image.open(png).convert("L") as im:
            return ImageStat.Stat(im).mean[0]
    finally:
        png.unlink(missing_ok=True)


def validate_output(path: Path, expected_duration: float) -> dict:
    """Verify the rendered file really contains the merged template.

    Checks stream presence, duration against the template, that the theme
    music is actually audible (not a silent track), and that the picture is
    not black after the dissolve (the photo overlay happened).
    """
    path = Path(path)
    info = _probe(path)
    streams = info.get("streams", [])
    for codec_type in ("video", "audio"):
        if not any(s.get("codec_type") == codec_type for s in streams):
            raise ValidationError(
                f"Rendered video has no {codec_type} stream — the merge with "
                "the pre-rendered template failed."
            )

    duration = float(info.get("format", {}).get("duration") or 0)
    if abs(duration - expected_duration) > DURATION_TOLERANCE_S:
        raise ValidationError(
            f"Rendered duration {duration:.2f}s differs from the template "
            f"({expected_duration:.2f}s) — wrong or truncated template."
        )

    # Theme music: measure the loudness of a window right after the dissolve.
    mean_db = _mean_volume_db(path, start=min(3.0, duration / 2), span=4.0)
    if mean_db < SILENCE_FLOOR_DB:
        raise ValidationError(
            f"Rendered audio is silent (mean {mean_db:.1f} dB) — the theme "
            "music from the template is missing. Is PRERENDER_PATH pointing "
            "at the real tribute-prerender.mp4?"
        )

    # Photo overlay: sample a frame after the dissolve has finished.
    luma = _frame_luma(path, at=duration * 0.7)
    if luma < 3:
        raise ValidationError(
            f"Rendered picture is black (mean luma {luma:.1f}/255) — the "
            "photo was not overlaid onto the template."
        )

    return {"duration": round(duration, 3), "audio_mean_db": round(mean_db, 1),
            "frame_luma": round(luma, 1)}


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


def make_tribute(name: str, image: Path, out: Path, date: str | None = None,
                 fade: float = 1.5, prerender: Path | None = None) -> Path:
    if not 0.2 <= fade <= 5:
        raise ValueError("fade must be between 0.2 and 5 seconds")
    prerender = Path(prerender or PRERENDER)
    validate_template(prerender)
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


def media_duration(path: Path) -> float:
    return _probe_duration(Path(path))


def validate_intro(path: Path) -> tuple[float, bool]:
    """Verify an uploaded intro clip; return (duration, has_audio)."""
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise IntroError("Intro video upload is missing or empty.")
    info = _probe(path)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise IntroError("Intro file has no video stream — is it a video?")
    duration = float(info.get("format", {}).get("duration") or 0)
    if duration < 0.5:
        raise IntroError(f"Intro is too short ({duration:.2f}s).")
    if duration > INTRO_MAX_DURATION_S:
        raise IntroError(
            f"Intro is {duration:.0f}s long; the limit is {INTRO_MAX_DURATION_S:.0f}s."
        )
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    return duration, has_audio


def merge_with_intro(intro: Path, tribute: Path, out: Path, gap: float = 1.0) -> float:
    """Concatenate intro + black gap + tribute into `out`; return total duration.

    The intro is normalized to 1920x1080 (scaled and padded, keeping aspect)
    and both parts are re-encoded, so any input format works.
    """
    intro = Path(intro)
    tribute = Path(tribute)
    intro_duration, intro_has_audio = validate_intro(intro)
    tribute_duration = _probe_duration(tribute)
    if not 0 <= gap <= 10:
        raise IntroError("Gap must be between 0 and 10 seconds.")

    inputs = ["-i", str(intro), "-i", str(tribute)]
    next_input = 2  # lavfi sources are appended with explicit index bookkeeping
    filters: list[str] = []
    vsegs: list[str] = []
    asegs: list[str] = []

    # Segment 1: intro, normalized to the tribute canvas.
    filters.append(
        f"[0:v]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease,"
        f"pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30000/1001,"
        "format=yuv420p[v0]"
    )
    vsegs.append("[v0]")
    if intro_has_audio:
        filters.append(
            "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a0]"
        )
        asegs.append("[a0]")
    else:
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={intro_duration:.3f}"]
        idx = next_input
        next_input += 1
        filters.append(f"[{idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a0]")
        asegs.append("[a0]")

    # Segment 2: optional silent black gap.
    if gap >= 0.05:
        inputs += ["-f", "lavfi", "-i", f"color=c=black:s={FRAME_W}x{FRAME_H}:r=30000/1001:d={gap:.3f}"]
        v_idx = next_input
        next_input += 1
        inputs += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={gap:.3f}"]
        a_idx = next_input
        next_input += 1
        filters.append(f"[{v_idx}:v]setsar=1,format=yuv420p[vg]")
        filters.append(f"[{a_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[ag]")
        vsegs.append("[vg]")
        asegs.append("[ag]")

    # Segment 3: the validated tribute.
    filters.append("[1:v]setsar=1,fps=30000/1001,format=yuv420p[vt]")
    vsegs.append("[vt]")
    filters.append("[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[at]")
    asegs.append("[at]")

    filters.append("".join(vsegs) + f"concat=n={len(vsegs)}:v=1:a=0[v]")
    filters.append("".join(asegs) + f"concat=n={len(asegs)}:v=0:a=1[a]")

    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", *inputs,
         "-filter_complex", ";".join(filters),
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart",
         str(out)],
        check=True,
    )
    return intro_duration + gap + tribute_duration


def validate_merged(path: Path, expected_duration: float, tribute_starts_at: float) -> dict:
    """Verify the final video: streams, total duration, and that the tribute
    part (theme audible, photo visible) actually made it into the merge."""
    path = Path(path)
    info = _probe(path)
    streams = info.get("streams", [])
    for codec_type in ("video", "audio"):
        if not any(s.get("codec_type") == codec_type for s in streams):
            raise ValidationError(f"Final video has no {codec_type} stream — merge failed.")

    duration = float(info.get("format", {}).get("duration") or 0)
    if abs(duration - expected_duration) > 1.0:
        raise ValidationError(
            f"Final duration {duration:.2f}s differs from expected "
            f"{expected_duration:.2f}s (intro + gap + tribute)."
        )

    window_start = min(tribute_starts_at + 2.0, max(duration - 4.0, 0.0))
    mean_db = _mean_volume_db(path, start=window_start, span=4.0)
    if mean_db < SILENCE_FLOOR_DB:
        raise ValidationError(
            f"Audio in the tribute part is silent (mean {mean_db:.1f} dB) — "
            "the theme music did not survive the merge."
        )

    # Sample a frame 70% of the way into the tribute part, clamped before EOF.
    at = min(tribute_starts_at + (duration - tribute_starts_at) * 0.7, max(duration - 0.5, 0.0))
    luma = _frame_luma(path, at=at)
    if luma < 3:
        raise ValidationError(
            f"Picture is black in the tribute part (mean luma {luma:.1f}/255)."
        )

    return {"duration": round(duration, 3), "audio_mean_db": round(mean_db, 1),
            "frame_luma": round(luma, 1)}
