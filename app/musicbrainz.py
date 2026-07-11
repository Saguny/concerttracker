import asyncio
import time
import aiohttp

_session: aiohttp.ClientSession | None = None
_last_req: float = 0.0
_RATE = 1.1  # seconds between requests (MB enforces 1/s)


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            base_url="https://musicbrainz.org",
            headers={"User-Agent": "concert-tracker/1.0 (contact@off-by-one.digital)"},
        )
    return _session


async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()


async def _throttle() -> None:
    global _last_req
    wait = _RATE - (time.monotonic() - _last_req)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_req = time.monotonic()


async def search_artist(name: str) -> str | None:
    """Returns MBID string or None."""
    from app.redis_client import get_redis

    cache_key = f"mb:artist:{name.lower()}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return cached

    await _throttle()
    try:
        async with _get_session().get(
            "/ws/2/artist",
            params={"query": name, "limit": 1, "fmt": "json"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    artists = data.get("artists", [])
    if not artists:
        return None

    mbid: str = artists[0].get("id")
    if mbid and r:
        await r.set(cache_key, mbid, ex=30 * 86400)

    return mbid
