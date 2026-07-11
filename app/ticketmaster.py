import os
import json
import aiohttp

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(base_url="https://app.ticketmaster.com")
    return _session


async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()


async def search_upcoming(artist: str, max_results: int = 50) -> list[dict]:
    from app.redis_client import get_redis

    api_key = os.environ.get("TICKETMASTER_API_KEY")
    if not api_key:
        return []

    cache_key = f"tm:search:{artist.lower()}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            cached_data = json.loads(cached)
            return cached_data

    page_size = 20  # Ticketmaster's page size for this fetch loop
    all_events: list[dict] = []
    page = 0
    total_pages = 1

    while page < total_pages and len(all_events) < max_results:
        try:
            async with _get_session().get(
                "/discovery/v2/events.json",
                params={
                    "apikey": api_key,
                    "keyword": artist,
                    "classificationName": "music",
                    "size": page_size,
                    "page": page,
                    "sort": "date,asc",
                },
            ) as resp:
                if resp.status != 200:
                    break
                data = await resp.json()
        except Exception:
            break

        events = data.get("_embedded", {}).get("events", [])
        page_info = data.get("page", {})
        total_pages = page_info.get("totalPages", 1)

        if not events:
            break

        all_events.extend(events)
        page += 1

    results = []
    for e in all_events[:max_results]:
        venues = e.get("_embedded", {}).get("venues", [{}])
        v = venues[0] if venues else {}
        start = e.get("dates", {}).get("start", {})
        results.append(
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "date": start.get("localDate"),
                "time": start.get("localTime"),
                "venue_name": v.get("name"),
                "city": v.get("city", {}).get("name"),
                "state": v.get("state", {}).get("stateCode"),
                "country": v.get("country", {}).get("countryCode"),
                "url": e.get("url"),
                "status": e.get("dates", {}).get("status", {}).get("code"),
            }
        )

    if r:
        await r.set(cache_key, json.dumps(results), ex=3600)

    return results


async def get_event_lineup(artist: str, date: str) -> list[str]:
    """Returns list of support act names for a given artist + date (YYYY-MM-DD)."""
    from app.redis_client import get_redis

    api_key = os.environ.get("TICKETMASTER_API_KEY")
    if not api_key:
        return []

    cache_key = f"tm:lineup:{artist.lower()}:{date}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)

    try:
        async with _get_session().get(
            "/discovery/v2/events.json",
            params={
                "apikey": api_key,
                "keyword": artist,
                "classificationName": "music",
                "startDateTime": f"{date}T00:00:00Z",
                "endDateTime": f"{date}T23:59:59Z",
                "size": 5,
            },
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []

    events = data.get("_embedded", {}).get("events", [])
    if not events:
        return []

    # Pick the first matching event and pull its full attraction/lineup list
    event = events[0]
    attractions = event.get("_embedded", {}).get("attractions", [])
    names = [a.get("name") for a in attractions if a.get("name")]

    # Exclude the headliner itself — everything else is a support act
    support = [n for n in names if n.lower() != artist.lower()]

    if r:
        await r.set(cache_key, json.dumps(support), ex=86400)

    return support