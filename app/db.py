import os
import json
import asyncpg

_pool: asyncpg.Pool | None = None


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def init_pool() -> asyncpg.Pool:
    global _pool
    _pool = await asyncpg.create_pool(
        os.environ["DATABASE_URL"],
        min_size=int(os.environ.get("DB_POOL_MIN", 2)),
        max_size=int(os.environ.get("DB_POOL_MAX", 10)),
        init=_init_conn,
    )
    await _migrate()
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    return _pool


async def _migrate() -> None:
    migrations_dir = os.path.join(os.path.dirname(__file__), "..", "migrations")
    async with _pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(7374297)")
        try:
            for filename in sorted(os.listdir(migrations_dir)):
                if filename.endswith(".sql"):
                    with open(os.path.join(migrations_dir, filename)) as f:
                        sql = f.read()
                    await conn.execute(sql)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(7374297)")
