import os
import re
import aiohttp

_session: aiohttp.ClientSession | None = None

def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()

def _key() -> str | None:
    return os.environ.get("LASTFM_API_KEY")

def _strip_html(text: str) -> str:
    text = re.sub(r"<a\b[^>]*>.*?</a>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()

async def get_artist_info(name: str) -> dict | None:
    key = _key()
    if not key:
        return None
    try:
        async with _get_session().get(
            "https://ws.audioscrobbler.com/2.0/",
            params={
                "method": "artist.getInfo",
                "artist": name,
                "api_key": key,
                "format": "json",
                "autocorrect": "1",
            },
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)

        artist = data.get("artist")
        if not artist:
            return None

        bio_raw = artist.get("bio", {}).get("summary", "")
        bio = _strip_html(bio_raw)

        stats = artist.get("stats", {})
        similar = [
            {"name": a["name"], "url": a["url"]}
            for a in (artist.get("similar", {}).get("artist") or [])[:5]
        ]
        tags = [t["name"] for t in (artist.get("tags", {}).get("tag") or [])[:5]]

        return {
            "bio": bio or None,
            "listeners": int(stats.get("listeners", 0)),
            "playcount": int(stats.get("playcount", 0)),
            "similar": similar,
            "tags": tags,
            "url": artist.get("url"),
        }
    except Exception:
        return None
