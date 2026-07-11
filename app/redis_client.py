import os
import redis.asyncio as aioredis

_client: aioredis.Redis | None = None


async def get_redis() -> aioredis.Redis | None:
    global _client
    if _client is not None:
        return _client
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    try:
        client = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=1)
        await client.ping()
        _client = client
        return _client
    except Exception:
        return None


async def close_redis() -> None:
    global _client
    if _client:
        await _client.aclose()
        _client = None
