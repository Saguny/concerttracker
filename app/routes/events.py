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

        shows = await conn.fetch(
            "SELECT s.id, s.artist, s.venue, s.city, s.date, s.rating, s.photo_url, "
            "s.artist_thumb_url, s.notes, u.username, u.avatar_url, "
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

    return templates.TemplateResponse(
        "event_detail.html",
        _ctx(
            request,
            user,
            event=event,
            shows=shows,
            avg_rating=avg_rating,
            rating_dist=rating_dist,
            max_rating_count=max_rating_count,
            hero_url=hero_url,
            user_show_id=user_show_id,
            today=time.strftime("%Y-%m-%d"),
            csrf=get_csrf_token(request),
        ),
    )
