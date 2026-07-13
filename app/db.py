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
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
            )
            applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}

            # Bootstrap: if this is the first time tracking runs but the DB already exists,
            # mark migrations 001-011 as applied (they ran before tracking was introduced).
            # We detect this by checking for festivals.event_id which migration 011 added.
            if not applied:
                col_exists = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
                    "WHERE table_name='festivals' AND column_name='event_id')"
                )
                if col_exists:
                    for filename in sorted(os.listdir(migrations_dir)):
                        if filename.endswith(".sql") and filename <= "011_festival_events.sql":
                            await conn.execute(
                                "INSERT INTO schema_migrations (filename) VALUES ($1) ON CONFLICT DO NOTHING",
                                filename,
                            )
                    applied = {r["filename"] for r in await conn.fetch("SELECT filename FROM schema_migrations")}

            for filename in sorted(os.listdir(migrations_dir)):
                if not filename.endswith(".sql") or filename in applied:
                    continue
                with open(os.path.join(migrations_dir, filename)) as f:
                    sql = f.read()
                await conn.execute(sql)
                await conn.execute("INSERT INTO schema_migrations (filename) VALUES ($1)", filename)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(7374297)")
