import os
import json
import asyncio
import aiohttp

_session: aiohttp.ClientSession | None = None

def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(headers={"Accept": "application/json"})
    return _session

async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()

def _key() -> str | None:
    return os.environ.get("SONGKICK_API_KEY")

async def _search_artist_id(name: str) -> int | None:
    key = _key()
    if not key:
        return None
    try:
        async with _get_session().get(
            "https://api.songkick.com/api/3.0/artists/search.json",
            params={"query": name, "apikey": key},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        results = data.get("resultsPage", {}).get("results", {}).get("artist", [])
        return results[0]["id"] if results else None
    except Exception:
        return None

async def search_upcoming(artist: str, limit: int = 8) -> list[dict]:
    """Returns upcoming event stubs for venue autocomplete."""
    from app.redis_client import get_redis

    key = _key()
    if not key:
        return []

    cache_key = f"songkick:upcoming:{artist.lower()}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)

    artist_id = await _search_artist_id(artist)
    if not artist_id:
        return []

    try:
        async with _get_session().get(
            f"https://api.songkick.com/api/3.0/artists/{artist_id}/calendar.json",
            params={"apikey": key, "per_page": limit},
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
    except Exception:
        return []

    events = data.get("resultsPage", {}).get("results", {}).get("event", []) or []
    results = []
    for e in events[:limit]:
        venue = e.get("venue") or {}
        loc = e.get("location") or {}
        city_str = loc.get("city", "")
        city = city_str.split(",")[0].strip() if city_str else ""
        state = city_str.split(",")[1].strip() if "," in city_str else ""
        results.append({
            "venue_name": venue.get("displayName", ""),
            "city": city,
            "state": state,
            "date": e.get("start", {}).get("date", ""),
            "url": e.get("uri", ""),
        })

    if r and results:
        await r.set(cache_key, json.dumps(results), ex=3600)
    return results

async def get_event_lineup(artist: str, date: str) -> list[str]:
    """Returns list of support act names for a given artist + date (YYYY-MM-DD)."""
    from app.redis_client import get_redis

    key = _key()
    if not key:
        return []

    cache_key = f"songkick:lineup:{artist.lower()}:{date}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)

    artist_id = await _search_artist_id(artist)
    if not artist_id:
        return []

                                                                          
    try:
        async with _get_session().get(
            f"https://api.songkick.com/api/3.0/artists/{artist_id}/calendar.json",
            params={"apikey": key, "min_date": date, "max_date": date, "per_page": 10},
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
    except Exception:
        return []

    events = data.get("resultsPage", {}).get("results", {}).get("event", []) or []
    if not events:
        return []

                                                                       
    event = events[0]
    performances = event.get("performance", []) or []
    support = [
        p["artist"]["displayName"]
        for p in performances
        if p.get("billing", "").lower() != "headline"
        and p.get("artist", {}).get("displayName")
    ]

    if r:
        await r.set(cache_key, json.dumps(support), ex=86400)
    return support