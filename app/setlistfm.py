import os
import json
import aiohttp

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            base_url="https://api.setlist.fm",
            headers={"Accept": "application/json"},
        )
    return _session


def _headers() -> dict:
    return {"x-api-key": os.environ.get("SETLISTFM_API_KEY", "")}


async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()


async def search(artist_name: str, date: str, artist_mbid: str | None = None) -> dict | None:
    """date: YYYY-MM-DD. Returns {songs, venue, city, url, id} or None."""
    from app.redis_client import get_redis

    cache_key = f"setlistfm:{artist_name.lower()}:{date.replace('-', '')}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)

    if not os.environ.get("SETLISTFM_API_KEY"):
        return None

    y, m, d = date.split("-")
    slfm_date = f"{d}-{m}-{y}"

    params: dict = {"p": 1}
    if artist_mbid:
        params["artistMbid"] = artist_mbid
    else:
        params["artistName"] = artist_name
    params["date"] = slfm_date

    try:
        async with _get_session().get("/rest/1.0/search/setlists", params=params, headers=_headers()) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    setlists = data.get("setlist", [])
    if not setlists:
        return None

    sl = setlists[0]
    songs = [
        song.get("name", "")
        for s in sl.get("sets", {}).get("set", [])
        for song in s.get("song", [])
        if song.get("name")
    ]

    result = {
        "id": sl.get("id"),
        "url": sl.get("url"),
        "songs": songs,
        "venue": sl.get("venue", {}).get("name"),
        "city": sl.get("venue", {}).get("city", {}).get("name"),
    }

    if r:
        await r.set(cache_key, json.dumps(result), ex=86400)

    return result


async def search_lineup(artist_name: str, date: str, artist_mbid: str | None = None) -> list[str]:
    """Backfill support acts for a *past* show via setlist.fm.

    Ticketmaster only carries forward-looking event data, so it can't answer
    "who else played this show" once the date is in the past. Setlist.fm has
    crowd-sourced setlists per-artist though, and each act on a bill usually
    gets its own setlist entry for the same venue/date -- so we first find
    the headliner's setlist to pin down the venue, then search that venue+date
    combo for every other setlist logged that night.

    Returns a list of support-act names (headliner excluded). Empty list if
    nothing found or the API key isn't configured.
    """
    from app.redis_client import get_redis

    cache_key = f"setlistfm:lineup:{artist_name.lower()}:{date.replace('-', '')}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)

    if not os.environ.get("SETLISTFM_API_KEY"):
        return []

    y, m, d = date.split("-")
    slfm_date = f"{d}-{m}-{y}"

    # Step 1: locate the headliner's own setlist to find the venue.
    headliner_params: dict = {"p": 1, "date": slfm_date}
    if artist_mbid:
        headliner_params["artistMbid"] = artist_mbid
    else:
        headliner_params["artistName"] = artist_name

    try:
        async with _get_session().get("/rest/1.0/search/setlists", params=headliner_params) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []

    setlists = data.get("setlist", [])
    if not setlists:
        return []

    venue = setlists[0].get("venue", {})
    venue_id = venue.get("id")
    venue_name = venue.get("name")
    if not venue_id and not venue_name:
        return []

    # Step 2: pull every setlist logged for that venue on that date -- each
    # support act that has a setlist.fm entry shows up here as its own result.
    lineup_params: dict = {"date": slfm_date}
    if venue_id:
        lineup_params["venueId"] = venue_id
    else:
        lineup_params["venueName"] = venue_name

    acts: list[str] = []
    seen = {artist_name.lower()}
    try:
        page = 1
        while page <= 3:  # a night's lineup is never more than a few pages
            lineup_params["p"] = page
            async with _get_session().get("/rest/1.0/search/setlists", params=lineup_params, headers=_headers()) as resp:
                if resp.status != 200:
                    break
                page_data = await resp.json()
            page_setlists = page_data.get("setlist", [])
            if not page_setlists:
                break
            for sl in page_setlists:
                name = sl.get("artist", {}).get("name")
                if name and name.lower() not in seen:
                    seen.add(name.lower())
                    acts.append(name)
            total = page_data.get("total", 0)
            per_page = page_data.get("itemsPerPage", 20)
            if page * per_page >= total:
                break
            page += 1
    except Exception:
        pass

    if r:
        await r.set(cache_key, json.dumps(acts), ex=86400)

    return acts