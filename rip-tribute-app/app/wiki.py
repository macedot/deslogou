"""Wikipedia photo lookup for a person's name.

Tries the pt Wikipedia summary API first, then en. Returns the article's
original photo. SVGs are rejected (Pillow can't rasterize them).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "rip-tribute-app/1.0 (tribute video generator)"
LANGS = ("pt", "en")


class PhotoNotFound(Exception):
    pass


def _summary(name: str, lang: str) -> dict:
    title = urllib.parse.quote(name.strip().replace(" ", "_"))
    url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def find_photo(name: str) -> dict:
    """Return {name, photo_url, page_url, lang} or raise PhotoNotFound."""
    for lang in LANGS:
        try:
            data = _summary(name, lang)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if data.get("type") == "disambiguation":
            continue
        photo_url = (data.get("originalimage") or {}).get("source")
        if not photo_url or photo_url.lower().split("?")[0].endswith(".svg"):
            continue
        page_url = (data.get("content_urls") or {}).get("desktop", {}).get("page")
        return {
            "name": data.get("title") or name,
            "photo_url": photo_url.split("?")[0],
            "page_url": page_url,
            "lang": lang,
        }
    raise PhotoNotFound(
        f"No Wikipedia photo found for '{name}'. Try the full name as written on Wikipedia."
    )
