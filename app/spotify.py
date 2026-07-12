import os
import base64
import json
import time
import aiohttp

_session: aiohttp.ClientSession | None = None
_token: str | None = None
_token_exp: float = 0.0

def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session

async def close() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()

async def _token_valid() -> str | None:
    global _token, _token_exp
    client_id = os.environ.get("SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    if _token and time.monotonic() < _token_exp:
        return _token

    creds = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    try:
        async with _get_session().post(
            "https://accounts.spotify.com/api/token",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    _token = data["access_token"]
    _token_exp = time.monotonic() + data["expires_in"] - 60
    return _token

async def search_artists(query: str, limit: int = 6) -> list[dict]:
    """Returns up to `limit` artist matches for autocomplete."""
    token = await _token_valid()
    if not token:
        return []
    try:
        async with _get_session().get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "type": "artist", "limit": limit},
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
    except Exception:
        return []

    results = []
    for a in data.get("artists", {}).get("items", []):
        images = sorted(a.get("images", []), key=lambda x: x.get("width", 0), reverse=True)
        results.append({
            "id": a["id"],
            "name": a["name"],
            "genres": a.get("genres", [])[:2],
            "thumb_url": images[-1]["url"] if images else None,
        })
    return results

async def search_artist(name: str) -> dict | None:
    """Returns {id, name, genres, image_url, thumb_url, popularity, spotify_url} or None."""
    from app.redis_client import get_redis

    cache_key = f"spotify:artist:{name.lower()}"
    r = await get_redis()
    if r:
        cached = await r.get(cache_key)
        if cached:
            return json.loads(cached)

    token = await _token_valid()
    if not token:
        return None

    try:
        async with _get_session().get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": name, "type": "artist", "limit": 1},
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception:
        return None

    items = data.get("artists", {}).get("items", [])
    if not items:
        return None

    a = items[0]
    images = sorted(a.get("images", []), key=lambda x: x.get("width", 0), reverse=True)

    result = {
        "id": a["id"],
        "name": a["name"],
        "genres": a.get("genres", []),
        "image_url": images[0]["url"] if images else None,
        "thumb_url": images[-1]["url"] if images else None,
        "popularity": a.get("popularity"),
        "spotify_url": a.get("external_urls", {}).get("spotify"),
    }

    if r:
        await r.set(cache_key, json.dumps(result), ex=7 * 86400)

    return result
