import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_pool
from app.auth import get_csrf_token, get_flashes, optional_user
from app.jinja import templates

router = APIRouter()


def _ctx(request: Request, user, **kw) -> dict:
    return {"request": request, "user": user, "flashes": get_flashes(request), **kw}


@router.get("/events/{event_id}", response_class=HTMLResponse)
async def event_detail(
    event_id: int,
    request: Request,
    pool=Depends(get_pool),
    user=Depends(optional_user),
):
    async with pool.acquire() as conn:
        event = await conn.fetchrow(
            "SELECT * FROM events WHERE id = $1", event_id
        )
        if not event:
            return RedirectResponse("/concert-tracker/social", status_code=302)

        is_festival = event["event_type"] == "festival"

        if is_festival:
            # Each "log" is a festivals row (one per user)
            logs = await conn.fetch(
                "SELECT f.id, f.festival_name, f.rating, f.festival_notes AS notes, "
                "f.created_at, u.username, u.avatar_url, "
                "COALESCE(lc.cnt, 0) AS like_count, COALESCE(cc.cnt, 0) AS comment_count "
                "FROM festivals f "
                "JOIN users u ON u.id = f.user_id "
                "LEFT JOIN ("
                "  SELECT s.festival_id, COUNT(*) AS cnt FROM show_likes sl "
                "  JOIN shows s ON s.id = sl.show_id GROUP BY s.festival_id"
                ") lc ON lc.festival_id = f.id "
                "LEFT JOIN ("
                "  SELECT s.festival_id, COUNT(*) AS cnt FROM show_comments sc "
                "  JOIN shows s ON s.id = sc.show_id GROUP BY s.festival_id"
                ") cc ON cc.festival_id = f.id "
                "WHERE f.event_id = $1 ORDER BY f.created_at DESC",
                event_id,
            )

            festival_ids = [r["id"] for r in logs]

            # Deduplicated artist lineup from all shows in all logged festivals
            lineup_rows = await conn.fetch(
                "SELECT DISTINCT s.artist, a.thumb_url "
                "FROM shows s "
                "LEFT JOIN artists a ON a.name = s.artist "
                "WHERE s.festival_id = ANY($1) AND s.artist IS NOT NULL "
                "ORDER BY s.artist",
                festival_ids,
            ) if festival_ids else []

            lineup = [{"name": r["artist"], "thumb": r["thumb_url"]} for r in lineup_rows]

            # Hero image: first artist thumb from lineup, or first user avatar
            hero_url = next((a["thumb"] for a in lineup if a["thumb"]), None)
            if not hero_url and logs:
                hero_url = logs[0]["avatar_url"]

            ratings = [float(r["rating"]) for r in logs if r["rating"] is not None]
            avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

            rating_dist = {i: 0 for i in range(1, 6)}
            for r in logs:
                if r["rating"] is not None:
                    bucket = max(1, min(5, round(float(r["rating"]))))
                    rating_dist[bucket] += 1
            max_rating_count = max(rating_dist.values()) if ratings else 1

            user_festival_id = None
            if user:
                for r in logs:
                    if r["username"] == user["username"]:
                        user_festival_id = r["id"]
                        break

            return templates.TemplateResponse(
                "event_detail.html",
                _ctx(
                    request, user,
                    event=event,
                    is_festival=True,
                    logs=logs,
                    shows=[],
                    lineup=lineup,
                    support_artists=[],
                    avg_rating=avg_rating,
                    rating_dist=rating_dist,
                    max_rating_count=max_rating_count,
                    hero_url=hero_url,
                    user_show_id=None,
                    user_festival_id=user_festival_id,
                    today=time.strftime("%Y-%m-%d"),
                    csrf=get_csrf_token(request),
                ),
            )

        # ── Regular show event ────────────────────────────────────────
        shows = await conn.fetch(
            "SELECT s.id, s.artist, s.venue, s.city, s.date, s.rating, s.photo_url, "
            "s.artist_thumb_url, s.notes, s.support_acts, u.username, u.avatar_url, "
            "COALESCE(lc.cnt, 0) AS like_count, COALESCE(cc.cnt, 0) AS comment_count "
            "FROM shows s "
            "JOIN users u ON u.id = s.user_id "
            "LEFT JOIN (SELECT show_id, COUNT(*) AS cnt FROM show_likes GROUP BY show_id) lc ON lc.show_id = s.id "
            "LEFT JOIN (SELECT show_id, COUNT(*) AS cnt FROM show_comments GROUP BY show_id) cc ON cc.show_id = s.id "
            "WHERE s.event_id = $1 ORDER BY s.created_at DESC",
            event_id,
        )
        ratings = [float(s["rating"]) for s in shows if s["rating"] is not None]
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

        rating_dist = {i: 0 for i in range(1, 6)}
        for s in shows:
            if s["rating"] is not None:
                bucket = max(1, min(5, round(float(s["rating"]))))
                rating_dist[bucket] += 1
        max_rating_count = max(rating_dist.values()) if ratings else 1

        hero_url = next((s["artist_thumb_url"] for s in shows if s["artist_thumb_url"]), None)
        if not hero_url:
            hero_url = next((s["photo_url"] for s in shows if s["photo_url"]), None)

        user_show_id = None
        if user:
            for s in shows:
                if s["username"] == user["username"]:
                    user_show_id = s["id"]
                    break

        support_tally: dict[str, int] = {}
        for s in shows:
            for name in (s["support_acts"] or []):
                support_tally[name] = support_tally.get(name, 0) + 1
        support_artists: list[dict] = []
        if support_tally:
            art_rows = await conn.fetch(
                "SELECT name, thumb_url FROM artists WHERE name = ANY($1)",
                list(support_tally.keys()),
            )
            thumb_map = {r["name"]: r["thumb_url"] for r in art_rows}
            support_artists = sorted(
                [{"name": n, "thumb": thumb_map.get(n), "count": c} for n, c in support_tally.items()],
                key=lambda x: -x["count"],
            )

    return templates.TemplateResponse(
        "event_detail.html",
        _ctx(
            request, user,
            event=event,
            is_festival=False,
            logs=[],
            shows=shows,
            lineup=[],
            support_artists=support_artists,
            avg_rating=avg_rating,
            rating_dist=rating_dist,
            max_rating_count=max_rating_count,
            hero_url=hero_url,
            user_show_id=user_show_id,
            user_festival_id=None,
            today=time.strftime("%Y-%m-%d"),
            csrf=get_csrf_token(request),
        ),
    )
