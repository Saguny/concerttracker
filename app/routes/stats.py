from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.db import get_pool
from app.auth import get_flashes, require_user
from app.jinja import templates

router = APIRouter()


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(
        "stats.html",
        {"request": request, "user": user, "flashes": get_flashes(request)},
    )


@router.get("/api/stats")
async def stats_data(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    uid = user["id"]

    async with pool.acquire() as conn:
        per_year = await conn.fetch(
            "SELECT EXTRACT(YEAR FROM date)::int AS year, COUNT(*)::int AS count "
            "FROM shows WHERE user_id=$1 GROUP BY year ORDER BY year", uid,
        )
        top_artists = await conn.fetch(
            "SELECT artist, COUNT(*)::int AS count FROM shows WHERE user_id=$1 "
            "GROUP BY artist ORDER BY count DESC LIMIT 10", uid,
        )
        top_venues = await conn.fetch(
            "SELECT venue, COUNT(*)::int AS count FROM shows WHERE user_id=$1 "
            "GROUP BY venue ORDER BY count DESC LIMIT 10", uid,
        )
        by_month = await conn.fetch(
            "SELECT EXTRACT(MONTH FROM date)::int AS month, COUNT(*)::int AS count "
            "FROM shows WHERE user_id=$1 GROUP BY month ORDER BY month", uid,
        )
        kind_split = await conn.fetch(
            "SELECT is_festival, COUNT(*)::int AS count FROM shows WHERE user_id=$1 GROUP BY is_festival", uid,
        )
        total = await conn.fetchval("SELECT COUNT(*) FROM shows WHERE user_id=$1", uid)
        upcoming = await conn.fetchval(
            "SELECT COUNT(*) FROM shows WHERE user_id=$1 AND date >= CURRENT_DATE", uid
        )
        unique_artists = await conn.fetchval(
            "SELECT COUNT(DISTINCT artist) FROM shows WHERE user_id=$1", uid
        )

    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    month_map = {r["month"]: r["count"] for r in by_month}

    festival_count = next((r["count"] for r in kind_split if r["is_festival"]), 0)
    standalone_count = next((r["count"] for r in kind_split if not r["is_festival"]), 0)

    return {
        "total": total,
        "upcoming": upcoming,
        "per_year": [{"year": r["year"], "count": r["count"]} for r in per_year],
        "top_artists": [{"artist": r["artist"], "count": r["count"]} for r in top_artists],
        "top_venues": [{"venue": r["venue"], "count": r["count"]} for r in top_venues],
        "by_month": [{"month": months[i], "count": month_map.get(i + 1, 0)} for i in range(12)],
        "festival": festival_count,
        "standalone": standalone_count,
        "unique_artists": unique_artists,
    }
