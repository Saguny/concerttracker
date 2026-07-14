import os
import time
import json

from typing import Optional
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db import get_pool
from app.auth import flash, get_csrf_token, get_flashes, require_user, optional_user
from app.jinja import templates
from app.routes.notifications import create_notification
import app.setlistfm as setlistfm
import app.spotify as spotify
import app.musicbrainz as musicbrainz
import app.ticketmaster as ticketmaster

_GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")

router = APIRouter()

_CITY_ALIASES: dict[str, str] = {
    "cologne": "Köln",
    "colgone": "Köln",
    "koeln": "Köln",
    "köln": "Köln",
}

async def _link_festival_event(conn, festival_id: int, festival_name: str, city: str | None) -> None:
    first_date = await conn.fetchval(
        "SELECT MIN(date) FROM shows WHERE festival_id = $1", festival_id
    )
    if not first_date:
        return
    year_str = str(first_date.year)
    # Key is name+year only — city varies too much between users for the same festival
    norm_key = f"{festival_name.lower()}|{year_str}"
    event_id = await conn.fetchval(
        "INSERT INTO events (normalized_key, artist, date, venue, city, event_type) "
        "VALUES ($1,$2,$3,$4,$5,'festival') ON CONFLICT (normalized_key) DO NOTHING RETURNING id",
        norm_key, festival_name, first_date, festival_name, city or None,
    )
    if event_id is None:
        event_id = await conn.fetchval(
            "SELECT id FROM events WHERE normalized_key = $1", norm_key
        )
    if event_id:
        await conn.execute(
            "UPDATE festivals SET event_id = $1 WHERE id = $2 AND event_id IS DISTINCT FROM $1",
            event_id, festival_id,
        )


def _normalize_city(city: str) -> str:
    city = city.strip()
    normalized = (
        city
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    )
    canonical = _CITY_ALIASES.get(normalized.lower())
    return canonical if canonical else city.strip()

def _ctx(request: Request, user: dict, **kw) -> dict:
    return {"request": request, "user": user, "flashes": get_flashes(request), **kw}

def _parse_show(row) -> dict | None:
    """Convert an asyncpg Record for a `shows` row into a plain dict with
    `setlist` decoded back into an object.

    `setlist` is a json/jsonb column and _handle_save() writes it with
    json.dumps(), but asyncpg returns json/jsonb columns as raw strings
    rather than parsing them -- there's no codec registered for it on this
    pool. Left as a string, `show.setlist.songs` in templates/JS silently
    resolves to nothing, so the setlist never renders even though it was
    saved correctly.
    """
    if row is None:
        return None
    show = dict(row)
    if isinstance(show.get("setlist"), str):
        try:
            show["setlist"] = json.loads(show["setlist"])
        except Exception:
            show["setlist"] = None
    return show

@router.get("/shows", response_class=HTMLResponse)
async def list_shows(
    request: Request,
    pool=Depends(get_pool),
    user=Depends(require_user),
    year: str = "",
    artist: str = "",
    kind: str = "",
    sort: str = "date_desc",
    page: int = 1,
):
    order = {
        "date_desc": "date DESC",
        "date_asc": "date ASC",
        "artist": "artist ASC",
        "venue": "venue ASC",
        "rating_desc": "rating DESC NULLS LAST",
    }.get(sort, "date DESC")

    clauses = ["user_id = $1"]
    params: list = [user["id"]]

    if year:
        params.append(int(year))
        clauses.append(f"EXTRACT(YEAR FROM date) = ${len(params)}")
    if artist:
        params.append(f"%{artist.lower()}%")
        clauses.append(f"LOWER(artist) LIKE ${len(params)}")
    if kind == "festival":
        clauses.append("is_festival = TRUE")
    elif kind == "standalone":
        clauses.append("is_festival = FALSE")

    where = " AND ".join(clauses)
    page_size = 50
    offset = (max(page, 1) - 1) * page_size
    count_params = list(params)
    sql = (
        f"SELECT s.*, "
        "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
        "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
        f"FROM shows s WHERE {where} ORDER BY {order} "
        f"LIMIT ${len(params)+1} OFFSET ${len(params)+2}"
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params, page_size, offset)
        total_count = await conn.fetchval(
            f"SELECT COUNT(*) FROM shows s WHERE {where}", *count_params
        )
        years = await conn.fetch(
            "SELECT DISTINCT EXTRACT(YEAR FROM date)::int AS y FROM shows WHERE user_id = $1 ORDER BY y DESC",
            user["id"],
        )

    seen_festivals: dict = {}
    items: list = []
    for row in rows:
        show = _parse_show(row)
        fid = show.get("festival_id")
        if show["is_festival"] and show.get("festival_name") and fid:
            key = str(fid)
            if key not in seen_festivals:
                entry: dict = {
                    "type": "festival",
                    "festival_id": key,
                    "festival_name": show["festival_name"],
                    "city": show["city"],
                    "date": show["date"],
                    "like_count": 0,
                    "comment_count": 0,
                    "shows": [],
                }
                seen_festivals[key] = entry
                items.append(entry)
            seen_festivals[key]["like_count"] += row["like_count"] or 0
            seen_festivals[key]["comment_count"] += row["comment_count"] or 0
            seen_festivals[key]["shows"].append(show)
        else:
            items.append({"type": "show", "show": show})

    total_pages = max(1, (total_count + page_size - 1) // page_size)
    return templates.TemplateResponse(
        "list.html",
        _ctx(
            request,
            user,
            items=items,
            years=[r["y"] for r in years],
            filters={"year": year, "artist": artist, "kind": kind, "sort": sort},
            today=time.strftime("%Y-%m-%d"),
            page=page,
            total_pages=total_pages,
            total_count=total_count,
            csrf=get_csrf_token(request),
        ),
    )

@router.get("/shows/festival/{festival_id}", response_class=HTMLResponse)
async def festival_detail(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user),
):
    async with pool.acquire() as conn:
        fest = await conn.fetchrow(
            "SELECT f.*, u.username AS owner_username, u.avatar_url AS owner_avatar "
            "FROM festivals f JOIN users u ON u.id = f.user_id WHERE f.id = $1",
            festival_id,
        )
        if not fest:
            flash(request, "Festival not found", "error")
            return RedirectResponse("/concert-tracker/shows", status_code=302)
        rows = await conn.fetch(
            "SELECT * FROM shows WHERE festival_id = $1 ORDER BY date ASC, artist ASC",
            festival_id,
        )
        is_own = fest["user_id"] == user["id"]
        owner_user = {"username": fest["owner_username"], "avatar_url": fest["owner_avatar"]} if not is_own else user
        festival_name = fest["festival_name"]
        festival_notes = fest["festival_notes"]
        show_ids = [r["id"] for r in rows]
        rep_id = rows[0]["id"] if rows else None
        attendees = await conn.fetch(
            "SELECT DISTINCT u.id, u.username, u.avatar_url FROM show_attendees sa "
            "JOIN users u ON u.id = sa.user_id WHERE sa.show_id = ANY($1)",
            show_ids,
        ) if show_ids else []
        already_tagged = {a["id"] for a in attendees}
        following = await conn.fetch(
            "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id WHERE f.user_id = $1",
            user["id"],
        )
        taggable = [f for f in following if f["id"] not in already_tagged] if is_own else []
        like_count = await conn.fetchval("SELECT COUNT(*) FROM show_likes WHERE show_id = $1", rep_id) if rep_id else 0
        user_liked = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", rep_id, user["id"]
        ) if rep_id else None
        comments = await conn.fetch(
            "SELECT c.id, c.body, c.created_at, u.username, u.avatar_url "
            "FROM show_comments c JOIN users u ON u.id = c.user_id "
            "WHERE c.show_id = $1 ORDER BY c.created_at ASC",
            rep_id,
        ) if rep_id else []
    shows = [_parse_show(r) for r in rows]
    return templates.TemplateResponse(
        "festival_detail.html",
        _ctx(
            request, user,
            festival_id=festival_id,
            festival_name=festival_name,
            city=shows[0]["city"],
            shows=shows,
            attendees=attendees,
            taggable=taggable,
            rep_id=rep_id,
            like_count=like_count,
            user_liked=bool(user_liked),
            comments=list(comments),
            festival_notes=festival_notes,
            festival_photo_url=fest["photo_url"],
            festival_event_id=fest["event_id"],
            owner_user=owner_user,
            is_own=is_own,
            csrf=get_csrf_token(request),
        ),
    )

@router.get("/shows/festival/{festival_id}/edit", response_class=HTMLResponse)
async def festival_edit_page(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT festival_name, city, festival_notes, rating, photo_url FROM festivals WHERE id = $1 AND user_id = $2",
            festival_id, user["id"],
        )
        if not row:
            flash(request, "Festival not found", "error")
            return RedirectResponse("/concert-tracker/shows", status_code=302)
        shows = await conn.fetch(
            "SELECT id, artist, date::text AS date, artist_thumb_url, rating FROM shows "
            "WHERE festival_id = $1 AND user_id = $2 ORDER BY date, artist",
            festival_id, user["id"],
        )
        show_ids = [s["id"] for s in shows]
        already_tagged = set()
        if show_ids:
            attendees = await conn.fetch(
                "SELECT DISTINCT user_id FROM show_attendees WHERE show_id = ANY($1)", show_ids
            )
            already_tagged = {a["user_id"] for a in attendees}
        following = await conn.fetch(
            "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id WHERE f.user_id = $1",
            user["id"],
        )
        taggable = [f for f in following if f["id"] not in already_tagged]
    existing_shows = [
        {"show_id": s["id"], "name": s["artist"], "date": s["date"], "thumb": s["artist_thumb_url"] or "",
         "rating": float(s["rating"]) if s["rating"] is not None else None}
        for s in shows
    ]
    return templates.TemplateResponse(
        "festival_edit.html",
        _ctx(request, user,
             festival_id=festival_id,
             festival_name=row["festival_name"],
             city=row["city"] or "",
             festival_notes=row["festival_notes"] or "",
             festival_rating=float(row["rating"]) if row["rating"] is not None else None,
             festival_photo_url=row["photo_url"],
             existing_shows=existing_shows,
             taggable=taggable,
             csrf=get_csrf_token(request)),
    )

@router.post("/shows/festival/{festival_id}/edit")
async def festival_edit_save(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    import asyncio, datetime as _dt
    await verify_csrf(request)
    form = await request.form()

    festival_name = str(form.get("festival_name", "")).strip()[:200]
    city = _normalize_city(str(form.get("city", ""))[:200])
    notes = str(form.get("festival_notes", "")).strip()[:2000] or None
    rating_raw = str(form.get("rating", "")).strip()
    festival_rating: float | None = None
    try:
        v = float(rating_raw)
        if 0.5 <= v <= 5.0:
            festival_rating = round(v * 2) / 2
    except (ValueError, TypeError):
        pass
    artists_raw = str(form.get("artists_json", "")).strip()

    submitted: list[dict] = []
    if artists_raw:
        try:
            submitted = [a for a in json.loads(artists_raw) if a.get("name") and a.get("date")]
        except Exception:
            pass

    if not festival_name:
        flash(request, "Festival name is required.", "error")
        return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}/edit", status_code=302)

    remove_photo = form.get("remove_photo") == "on"
    _fest_photo_data: bytes | None = None
    _fest_photo_ct: str | None = None
    photo_file = form.get("photo")
    if not remove_photo and photo_file and hasattr(photo_file, "filename") and photo_file.filename:
        _fest_photo_data = await photo_file.read()
        _fest_photo_ct = photo_file.content_type or "application/octet-stream"
        if len(_fest_photo_data) > 15 * 1024 * 1024:
            flash(request, "Photo too large (max 15 MB)", "error")
            return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}/edit", status_code=302)

    now = int(time.time())
    new_artists = [a for a in submitted if not a.get("show_id")]

    sp_results = []
    if new_artists:
        sp_results = list(await asyncio.gather(
            *[spotify.search_artist(a["name"]) for a in new_artists], return_exceptions=True
        ))

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM festivals WHERE id = $1 AND user_id = $2", festival_id, user["id"])
        if not row:
            flash(request, "Festival not found", "error")
            return RedirectResponse("/concert-tracker/shows", status_code=302)

        await conn.execute(
            "UPDATE festivals SET festival_name = $1, city = $2, festival_notes = $3, rating = $4 WHERE id = $5",
            festival_name, city, notes, festival_rating, festival_id,
        )
        if remove_photo:
            await conn.execute("UPDATE festivals SET photo_url = NULL WHERE id = $1", festival_id)
        elif _fest_photo_data:
            from app.r2 import upload_festival_photo as _up_fp
            try:
                _fp_url = await _up_fp(festival_id, _fest_photo_data, _fest_photo_ct)
                await conn.execute("UPDATE festivals SET photo_url = $1 WHERE id = $2", _fp_url, festival_id)
            except Exception:
                pass
        await conn.execute(
            "UPDATE shows SET festival_name = $1, venue = $1, city = $2 WHERE festival_id = $3 AND user_id = $4",
            festival_name, city, festival_id, user["id"],
        )

        kept_ids = [int(a["show_id"]) for a in submitted if a.get("show_id")]
        if kept_ids:
            await conn.execute(
                "DELETE FROM shows WHERE festival_id = $1 AND user_id = $2 AND id <> ALL($3)",
                festival_id, user["id"], kept_ids,
            )
            for a in submitted:
                if a.get("show_id"):
                    _sr = None
                    try:
                        v = float(a.get("rating") or 0)
                        if 0.5 <= v <= 5.0:
                            _sr = round(v * 2) / 2
                    except (TypeError, ValueError):
                        pass
                    await conn.execute(
                        "UPDATE shows SET date = $1, rating = $2 WHERE id = $3 AND festival_id = $4",
                        _dt.date.fromisoformat(str(a["date"])), _sr, int(a["show_id"]), festival_id,
                    )
        else:
            await conn.execute(
                "DELETE FROM shows WHERE festival_id = $1 AND user_id = $2", festival_id, user["id"]
            )

        for a, sp in zip(new_artists, sp_results):
            sp = sp if isinstance(sp, dict) else None
            _sr = None
            try:
                v = float(a.get("rating") or 0)
                if 0.5 <= v <= 5.0:
                    _sr = round(v * 2) / 2
            except (TypeError, ValueError):
                pass
            await conn.execute(
                "INSERT INTO shows (user_id, artist, venue, city, date, is_festival, festival_name, festival_id, "
                "artist_spotify_id, artist_image_url, artist_thumb_url, artist_genres, rating, created_at) "
                "VALUES ($1,$2,$3,$4,$5,TRUE,$6,$7,$8,$9,$10,$11,$12,$13)",
                user["id"], str(a["name"])[:200], festival_name, city,
                _dt.date.fromisoformat(str(a["date"])), festival_name, festival_id,
                sp["id"] if sp else None,
                sp["image_url"] if sp else None,
                sp["thumb_url"] if sp else None,
                sp["genres"] if sp else [],
                _sr,
                now,
            )

    tag_ids_raw = str(form.get("_tag_friend_ids", "")).strip()
    tag_uids: list[int] = []
    if tag_ids_raw:
        try:
            tag_uids = [int(v) for v in json.loads(tag_ids_raw) if str(v).strip()]
        except (ValueError, TypeError, json.JSONDecodeError):
            tag_uids = []
    if tag_uids:
        async with pool.acquire() as conn:
            show_ids = await conn.fetch(
                "SELECT id FROM shows WHERE festival_id = $1 AND user_id = $2", festival_id, user["id"]
            )
            for s in show_ids:
                for tag_uid in tag_uids:
                    await conn.execute(
                        "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        s["id"], tag_uid,
                    )
                await conn.execute(
                    "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    s["id"], user["id"],
                )

    async with pool.acquire() as conn:
        await _link_festival_event(conn, festival_id, festival_name, city)

    flash(request, "Festival updated!", "success")
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.post("/shows/festival/{festival_id}/like")
async def festival_like(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    owner_id = None
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM shows WHERE festival_id = $1 ORDER BY date ASC, artist ASC LIMIT 1",
            festival_id,
        )
        if not row:
            if _is_ajax(request):
                return JSONResponse({"error": "not found"}, status_code=404)
            return RedirectResponse("/concert-tracker/shows", status_code=302)
        show_id = row["id"]
        existing = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"]
        )
        if existing:
            await conn.execute("DELETE FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"])
            liked = False
        else:
            await conn.execute(
                "INSERT INTO show_likes (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                show_id, user["id"],
            )
            liked = True
            owner_id = await conn.fetchval("SELECT user_id FROM festivals WHERE id = $1", festival_id)
        count = await conn.fetchval("SELECT COUNT(*) FROM show_likes WHERE show_id = $1", show_id)
    if liked and owner_id:
        await create_notification(pool, user_id=owner_id, actor_id=user["id"], type="like",
                                  show_id=show_id, festival_id=festival_id)
    if _is_ajax(request):
        return JSONResponse({"liked": liked, "count": int(count)})
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)

@router.post("/shows/festival/{festival_id}/comments")
async def festival_add_comment(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    form = await request.form()
    body = str(form.get("body", "")).strip()[:500]
    comment_row = None
    owner_id = None
    comment_id = None
    show_id = None
    if body:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM shows WHERE festival_id = $1 ORDER BY date ASC, artist ASC LIMIT 1",
                festival_id,
            )
            if row:
                show_id = row["id"]
                now = int(time.time())
                comment_id = await conn.fetchval(
                    "INSERT INTO show_comments (show_id, user_id, body, created_at) VALUES ($1, $2, $3, $4) RETURNING id",
                    show_id, user["id"], body, now,
                )
                owner_id = await conn.fetchval("SELECT user_id FROM festivals WHERE id = $1", festival_id)
                comment_row = {"id": comment_id, "body": _render_mentions(body), "created_at": now,
                               "username": user["username"], "avatar_url": user.get("avatar_url")}
    if owner_id and comment_id:
        await create_notification(pool, user_id=owner_id, actor_id=user["id"],
                                  type="comment", show_id=show_id, festival_id=festival_id, comment_id=comment_id)
    if _is_ajax(request):
        return JSONResponse(comment_row or {"error": "empty"})
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)

@router.post("/shows/festival/{festival_id}/comments/{comment_id}/delete")
async def festival_delete_comment(
    festival_id: int, comment_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM show_comments WHERE id = $1 AND user_id = $2", comment_id, user["id"]
        )
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)

@router.post("/shows/festival/{festival_id}/tag")
async def tag_festival_friend(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()
    friend_id = int(form.get("friend_id", 0))
    if not friend_id:
        return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)
    tagged = False
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM shows WHERE festival_id = $1 AND user_id = $2", festival_id, user["id"])
        for row in rows:
            await conn.execute(
                "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                row["id"], friend_id,
            )
        if rows:
            tagged = True
    if tagged:
        await create_notification(pool, user_id=friend_id, actor_id=user["id"], type="tag", festival_id=festival_id)
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)

@router.post("/shows/festival/{festival_id}/untag")
async def untag_festival_friend(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()
    friend_id = int(form.get("friend_id", 0))
    if not friend_id:
        return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM shows WHERE festival_id = $1 AND user_id = $2", festival_id, user["id"])
        show_ids = [r["id"] for r in rows]
        await conn.execute(
            "DELETE FROM show_attendees WHERE show_id = ANY($1) AND user_id = $2",
            show_ids, friend_id,
        )
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)

@router.post("/shows/festival/{festival_id}/delete")
async def delete_festival(
    festival_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    from app.auth import verify_csrf
    await verify_csrf(request)
    async with pool.acquire() as conn:
        name_row = await conn.fetchrow(
            "SELECT festival_name FROM festivals WHERE id = $1 AND user_id = $2",
            festival_id, user["id"],
        )
        display_name = name_row["festival_name"] if name_row else "Festival"
        await conn.execute("DELETE FROM shows WHERE festival_id = $1", festival_id)
        await conn.execute(
            "DELETE FROM festivals WHERE id = $1 AND user_id = $2",
            festival_id, user["id"],
        )
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    flash(request, f"{display_name} deleted", "info")
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.get("/shows/add-festival", response_class=HTMLResponse)
async def add_festival_page(request: Request, user=Depends(require_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        friends = await conn.fetch(
            "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id WHERE f.user_id = $1",
            user["id"],
        )
    return templates.TemplateResponse(
        "festival_form.html",
        _ctx(request, user, friends=friends, csrf=get_csrf_token(request)),
    )

@router.post("/shows/add-festival")
async def add_festival(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()

    import json as _json
    import datetime

    festival_name = str(form.get("festival_name", "")).strip()[:200]
    venue = festival_name
    city = _normalize_city(str(form.get("city", ""))[:200])
    festival_notes = str(form.get("festival_notes", "")).strip()[:2000] or None
    rating_raw = str(form.get("rating", "")).strip()
    festival_rating: float | None = None
    try:
        v = float(rating_raw)
        if 0.5 <= v <= 5.0:
            festival_rating = round(v * 2) / 2
    except (ValueError, TypeError):
        pass
    artists_raw = str(form.get("artists_json", "")).strip()

    selected: list[dict] = []
    if artists_raw:
        try:
            selected = [
                a for a in _json.loads(artists_raw)
                if a.get("name") and a.get("date")
            ]
        except Exception:
            pass

    if not selected or not festival_name:
        flash(request, "Select at least one artist and fill in the festival details.", "error")
        return RedirectResponse("/concert-tracker/shows/add-festival", status_code=302)

    _new_photo_data: bytes | None = None
    _new_photo_ct: str | None = None
    new_photo_file = form.get("photo")
    if new_photo_file and hasattr(new_photo_file, "filename") and new_photo_file.filename:
        _new_photo_data = await new_photo_file.read()
        _new_photo_ct = new_photo_file.content_type or "application/octet-stream"
        if len(_new_photo_data) > 15 * 1024 * 1024:
            flash(request, "Photo too large (max 15 MB)", "error")
            return RedirectResponse("/concert-tracker/shows/add-festival", status_code=302)

    now = int(time.time())

    import asyncio

    sp_results, sl_results = await asyncio.gather(
        asyncio.gather(*[spotify.search_artist(a["name"]) for a in selected], return_exceptions=True),
        asyncio.gather(*[setlistfm.search(a["name"], str(a["date"])) for a in selected], return_exceptions=True),
    )

    async with pool.acquire() as conn:
        festival_id = await conn.fetchval(
            "INSERT INTO festivals (user_id, festival_name, city, festival_notes, rating, created_at) VALUES ($1,$2,$3,$4,$5,$6) RETURNING id",
            user["id"], festival_name, city, festival_notes, festival_rating, now,
        )
        if _new_photo_data and festival_id:
            from app.r2 import upload_festival_photo as _up_fp
            try:
                _fp_url = await _up_fp(festival_id, _new_photo_data, _new_photo_ct)
                await conn.execute("UPDATE festivals SET photo_url = $1 WHERE id = $2", _fp_url, festival_id)
            except Exception:
                pass
        for artist_data, sp, sl in zip(selected, sp_results, sl_results):
            sp = sp if isinstance(sp, dict) else None
            sl = sl if isinstance(sl, dict) else None
            artist_name = str(artist_data["name"])[:200]
            date = datetime.date.fromisoformat(str(artist_data["date"]))
            setlist_data = {"songs": sl["songs"], "url": sl.get("url"), "id": sl.get("id")} if sl and sl.get("songs") else None
            await conn.execute(
                "INSERT INTO shows (user_id, artist, venue, city, date, is_festival, festival_name, festival_id, "
                "artist_spotify_id, artist_image_url, artist_thumb_url, artist_genres, setlist, created_at) "
                "VALUES ($1,$2,$3,$4,$5,TRUE,$6,$7,$8,$9,$10,$11,$12,$13)",
                user["id"], artist_name, venue, city, date, festival_name, festival_id,
                sp["id"] if sp else None,
                sp["image_url"] if sp else None,
                sp["thumb_url"] if sp else None,
                sp["genres"] if sp else [],
                json.dumps(setlist_data) if setlist_data else None,
                now,
            )
            await _upsert_artist(conn, artist_name, sp, now)

        tag_ids_raw = str(form.get("tag_friend_ids", "")).strip()
        tag_uids: list[int] = []
        if tag_ids_raw:
            try:
                tag_uids = [int(v) for v in json.loads(tag_ids_raw) if str(v).strip()]
            except (ValueError, TypeError, json.JSONDecodeError):
                tag_uids = []
        if tag_uids:
            show_rows = await conn.fetch(
                "SELECT id FROM shows WHERE festival_id = $1", festival_id
            )
            for s in show_rows:
                for tag_uid in tag_uids:
                    await conn.execute(
                        "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        s["id"], tag_uid,
                    )
                await conn.execute(
                    "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    s["id"], user["id"],
                )

        await _link_festival_event(conn, festival_id, festival_name, city)

    flash(request, f"Logged {len(selected)} set{'s' if len(selected) != 1 else ''}!", "success")
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_id}", status_code=302)

@router.get("/shows/add", response_class=HTMLResponse)
async def add_show_page(request: Request, user=Depends(require_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        friends = await conn.fetch(
            "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id WHERE f.user_id = $1",
            user["id"],
        )
    return templates.TemplateResponse(
        "show_form.html",
        _ctx(request, user, show=None, friends=friends, csrf=get_csrf_token(request), errors={}, google_places_key=_GOOGLE_PLACES_KEY),
    )

@router.post("/shows/add")
async def add_show(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    if redirect := await _handle_save(request, pool, user, show_id=None):
        return redirect
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.get("/shows/{show_id:int}", response_class=HTMLResponse)
async def show_detail(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(optional_user)):
    async with pool.acquire() as conn:
        show = await conn.fetchrow(
            "SELECT s.*, u.username AS owner_username, u.avatar_url AS owner_avatar "
            "FROM shows s JOIN users u ON u.id = s.user_id WHERE s.id = $1",
            show_id,
        )
        if not show:
            if user:
                flash(request, "Show not found", "error")
            return RedirectResponse("/concert-tracker/social" if user else "/concert-tracker/login", status_code=302)
        show = _parse_show(show)
        is_owner = user is not None and show["user_id"] == user["id"]
        attendees = await conn.fetch(
            "SELECT u.username FROM show_attendees sa JOIN users u ON u.id = sa.user_id "
            "WHERE sa.show_id = $1 AND sa.user_id <> $2",
            show_id, show["user_id"],
        )
        like_count = await conn.fetchval("SELECT COUNT(*) FROM show_likes WHERE show_id = $1", show_id)
        user_liked = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"]
        ) if user else None
        comments = await conn.fetch(
            "SELECT c.id, c.body, c.created_at, u.username, u.avatar_url "
            "FROM show_comments c JOIN users u ON u.id = c.user_id "
            "WHERE c.show_id = $1 ORDER BY c.created_at ASC",
            show_id,
        )
        already_tagged = {a["username"] for a in attendees}
        taggable_friends = []
        if is_owner:
            following = await conn.fetch(
                "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id "
                "WHERE f.user_id = $1",
                user["id"],
            )
            taggable_friends = [f for f in following if f["username"] not in already_tagged]

        support_artists = []
        if show["support_acts"]:
            artist_rows = await conn.fetch(
                "SELECT name, thumb_url FROM artists WHERE name = ANY($1)",
                show["support_acts"],
            )
            by_name = {r["name"]: r["thumb_url"] for r in artist_rows}
            missing = [n for n in show["support_acts"] if not by_name.get(n)]
            if missing:
                import asyncio as _asyncio
                sp_results = await _asyncio.gather(
                    *[spotify.search_artist(n) for n in missing], return_exceptions=True
                )
                now = int(time.time())
                for name, sp in zip(missing, sp_results):
                    sp = sp if isinstance(sp, dict) else None
                    await _upsert_artist(conn, name, sp, now)
                    if sp:
                        by_name[name] = sp.get("thumb_url")
            support_artists = [
                {"name": n, "thumb": by_name.get(n)}
                for n in show["support_acts"]
            ]
    return templates.TemplateResponse(
        "show_detail.html",
        _ctx(request, user, show=show, attendees=attendees, is_owner=is_owner,
             like_count=like_count, user_liked=bool(user_liked),
             comments=list(comments), support_artists=support_artists,
             taggable_friends=taggable_friends,
             csrf=get_csrf_token(request) if user else ""),
    )

@router.get("/shows/{show_id}/edit", response_class=HTMLResponse)
async def edit_show_page(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1 AND user_id = $2", show_id, user["id"])
        if not show:
            return RedirectResponse("/concert-tracker/shows", status_code=302)
        show = _parse_show(show)
        friends = await conn.fetch(
            "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id WHERE f.user_id = $1",
            user["id"],
        )
    return templates.TemplateResponse(
        "show_form.html",
        _ctx(request, user, show=show, friends=friends, csrf=get_csrf_token(request), errors={}, google_places_key=_GOOGLE_PLACES_KEY),
    )

@router.post("/shows/{show_id}/edit")
async def edit_show(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    if redirect := await _handle_save(request, pool, user, show_id=show_id):
        return redirect
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.post("/shows/{show_id}/delete")
async def delete_show(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shows WHERE id = $1 AND user_id = $2", show_id, user["id"])
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    flash(request, "Show deleted", "info")
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.post("/shows/{show_id}/tag")
async def tag_friend(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    friend_id = int(form.get("friend_id", 0))
    tagged = False
    if friend_id:
        async with pool.acquire() as conn:
            show = await conn.fetchrow("SELECT id FROM shows WHERE id = $1 AND user_id = $2", show_id, user["id"])
            if show:
                await conn.execute(
                    "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    show_id, friend_id,
                )
                await conn.execute(
                    "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    show_id, user["id"],
                )
                tagged = True
    if tagged:
        await create_notification(pool, user_id=friend_id, actor_id=user["id"], type="tag", show_id=show_id)
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)

@router.get("/shows/export")
async def export_shows(
    request: Request, pool=Depends(get_pool), user=Depends(require_user),
    format: str = "csv",
):
    import csv, io, datetime as _dt
    from fastapi.responses import Response, StreamingResponse

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT artist, venue, city, date, is_festival, festival_name, notes, created_at "
            "FROM shows WHERE user_id=$1 ORDER BY date DESC",
            user["id"],
        )

    if format == "json":
        data = [
            {
                "artist": r["artist"], "venue": r["venue"], "city": r["city"],
                "date": str(r["date"]), "is_festival": r["is_festival"],
                "festival_name": r["festival_name"], "notes": r["notes"],
            }
            for r in rows
        ]
        return JSONResponse(data, headers={"Content-Disposition": 'attachment; filename="shows.json"'})

    if format == "ical":
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ConcertTracker//EN"]
        for r in rows:
            uid_str = f"{r['artist']}-{r['date']}-{r['venue']}@concerttracker".replace(" ", "_")
            dt = r["date"].strftime("%Y%m%d")
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uid_str}",
                f"DTSTART;VALUE=DATE:{dt}",
                f"SUMMARY:{r['artist']} @ {r['venue']}",
                f"LOCATION:{r['venue']}, {r['city']}",
                "END:VEVENT",
            ]
        lines.append("END:VCALENDAR")
        body = "\r\n".join(lines)
        return Response(body, media_type="text/calendar",
                        headers={"Content-Disposition": 'attachment; filename="shows.ics"'})

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Artist", "Venue", "City", "Date", "Festival", "Festival Name", "Notes"])
    for r in rows:
        writer.writerow([r["artist"], r["venue"], r["city"], r["date"],
                         "Yes" if r["is_festival"] else "No", r["festival_name"] or "", r["notes"] or ""])
    buf.seek(0)
    return Response(buf.read(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="shows.csv"'})

@router.get("/api/shows/{show_id}/likes")
async def show_likes(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT u.id, u.username, u.avatar_url FROM show_likes l "
            "JOIN users u ON u.id = l.user_id WHERE l.show_id=$1 ORDER BY u.username",
            show_id,
        )
    return [{"username": r["username"], "avatar_url": r["avatar_url"]} for r in rows]

@router.get("/api/setlistfm")
async def api_setlistfm(request: Request, artist: str = "", date: str = "", _=Depends(require_user)):
    if not artist or not date:
        return {"error": "artist and date required"}
    mbid = await musicbrainz.search_artist(artist)
    result = await setlistfm.search(artist, date, artist_mbid=mbid)
    if not result:
        return {"found": False}
    return {"found": True, **result}

@router.get("/api/upcoming")
async def api_upcoming(request: Request, artist: str = "", _=Depends(require_user)):
    if not artist:
        return []
    return await ticketmaster.search_upcoming(artist)

@router.get("/api/lineup")
async def api_lineup(request: Request, artist: str = "", date: str = "", _=Depends(require_user)):
    if not artist or not date:
        return []
    return await ticketmaster.get_event_lineup(artist, date)

@router.get("/api/lineup-setlistfm")
async def api_lineup_setlistfm(request: Request, artist: str = "", date: str = "", _=Depends(require_user)):
    """Fallback used by the show form when Ticketmaster has nothing for a
    past date -- Ticketmaster only carries forward-looking event data, so it
    can't backfill who else played a show that already happened.
    """
    if not artist or not date:
        return []
    mbid = await musicbrainz.search_artist(artist)
    return await setlistfm.search_lineup(artist, date, artist_mbid=mbid)

@router.get("/api/artist-search")
async def api_artist_search(request: Request, q: str = "", _=Depends(require_user)):
    if len(q) < 2:
        return []
    return await spotify.search_artists(q)

@router.get("/api/artist-info")
async def api_artist_info(request: Request, artist: str = "", _=Depends(require_user)):
    if not artist:
        return {}
    info = await spotify.search_artist(artist)
    return info or {}

                                                                               

async def _upsert_artist(conn, name: str, sp: dict | None, now: int) -> None:
    await conn.execute(
        """
        INSERT INTO artists (name, spotify_id, image_url, thumb_url, genres, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $6)
        ON CONFLICT (name) DO UPDATE SET
            spotify_id  = COALESCE(EXCLUDED.spotify_id,  artists.spotify_id),
            image_url   = COALESCE(EXCLUDED.image_url,   artists.image_url),
            thumb_url   = COALESCE(EXCLUDED.thumb_url,   artists.thumb_url),
            genres      = COALESCE(EXCLUDED.genres,      artists.genres),
            updated_at  = EXCLUDED.updated_at
        """,
        name,
        sp["id"]        if sp else None,
        sp["image_url"] if sp else None,
        sp["thumb_url"] if sp else None,
        sp["genres"]    if sp else None,
        now,
    )

async def _handle_save(request: Request, pool, user: dict, show_id: int | None):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()

    import datetime
    artist = str(form.get("artist", "")).strip()[:200]
    venue = str(form.get("venue", "")).strip()[:200]
    city = _normalize_city(str(form.get("city", ""))[:200])
    try:
        date = datetime.date.fromisoformat(str(form.get("date", "")).strip())
    except ValueError:
        flash(request, "Invalid date - please use the date picker.", "error")
        back = f"/concert-tracker/shows/{show_id}/edit" if show_id else "/concert-tracker/shows/add"
        return RedirectResponse(back, status_code=302)
    is_festival = form.get("is_festival") == "on"
    festival_name = str(form.get("festival_name", "")).strip()[:200] or None
    notes = str(form.get("notes", "")).strip()[:1000] or None
    setlist_raw = str(form.get("setlist_json", "")).strip() or None
    support_raw = str(form.get("support_acts_json", "")).strip() or None
    rating_raw = str(form.get("rating", "")).strip()
    rating: float | None = None
    if rating_raw:
        try:
            v = float(rating_raw)
            if 0.5 <= v <= 5.0 and round(v * 2) == int(v * 2):
                rating = v
        except ValueError:
            pass

    import json as _json
    setlist_data = None
    if setlist_raw:
        try:
            setlist_data = _json.loads(setlist_raw)
        except Exception:
            pass

    support_acts: list[str] = []
    if support_raw:
        try:
            support_acts = [s for s in _json.loads(support_raw) if isinstance(s, str)]
        except Exception:
            pass

    headliners_raw = str(form.get("headliners_json", "")).strip() or None
    co_headliners: list[str] = []
    if headliners_raw:
        try:
            co_headliners = [s for s in _json.loads(headliners_raw) if isinstance(s, str) and s.strip()]
        except Exception:
            pass
    headliners: list[str] = []
    if artist:
        seen: set[str] = {artist.lower()}
        headliners = [artist]
        for h in co_headliners:
            if h.lower() not in seen:
                headliners.append(h)
                seen.add(h.lower())

                                       
    import asyncio
    mbid_task = asyncio.create_task(musicbrainz.search_artist(artist))
    spotify_task = asyncio.create_task(spotify.search_artist(artist))
    mbid, sp = await asyncio.gather(mbid_task, spotify_task, return_exceptions=True)
    if isinstance(mbid, Exception):
        mbid = None
    if isinstance(sp, Exception):
        sp = None

    image_url = sp["image_url"] if sp else None
    thumb_url = sp["thumb_url"] if sp else None
    spotify_id = sp["id"] if sp else None
    genres = sp["genres"] if sp else []

    now = int(time.time())

                                                          
    _photo_data: bytes | None = None
    _photo_ct: str | None = None
    remove_photo = form.get("remove_photo") == "on"
    photo_file = form.get("photo")
    if not remove_photo and photo_file and hasattr(photo_file, "filename") and photo_file.filename:
        _photo_data = await photo_file.read()
        _photo_ct = photo_file.content_type or "application/octet-stream"
        if len(_photo_data) > 15 * 1024 * 1024:
            flash(request, "Photo too large (max 15 MB)", "error")
            back = f"/concert-tracker/shows/{show_id}/edit" if show_id else "/concert-tracker/shows/add"
            return RedirectResponse(back, status_code=302)

                                                                 
    support_sp_results = await asyncio.gather(
        *[spotify.search_artist(name) for name in support_acts],
        return_exceptions=True,
    ) if support_acts else []

    async with pool.acquire() as conn:
        if show_id is None:
            existing_id = await conn.fetchval(
                "SELECT id FROM shows WHERE user_id = $1 AND LOWER(artist) = LOWER($2) AND date = $3 LIMIT 1",
                user["id"], artist, date,
            )
            if existing_id:
                flash(
                    request,
                    f'You already logged this show. <a href="/concert-tracker/shows/{existing_id}">View it here →</a>',
                    "warning",
                )
                return RedirectResponse("/concert-tracker/shows/add", status_code=302)
            show_id = await conn.fetchval(
                "INSERT INTO shows (user_id, artist, venue, city, date, is_festival, festival_name, "
                "notes, setlist, support_acts, artist_mbid, artist_spotify_id, artist_image_url, "
                "artist_thumb_url, artist_genres, created_at, rating, headliners) VALUES "
                "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) RETURNING id",
                user["id"], artist, venue, city, date, is_festival, festival_name,
                notes, _json.dumps(setlist_data) if setlist_data else None,
                support_acts or None,
                mbid, spotify_id, image_url, thumb_url, genres, now, rating,
                headliners or None,
            )
                                                            
            if _photo_data and show_id:
                from app.r2 import upload_show_photo as _up
                try:
                    _new_url = await _up(show_id, _photo_data, _photo_ct)
                    await conn.execute("UPDATE shows SET photo_url=$1 WHERE id=$2", _new_url, show_id)
                except Exception:
                    pass
        else:
                                                                                
            _new_photo_url = None
            if _photo_data:
                from app.r2 import upload_show_photo as _up
                try:
                    _new_photo_url = await _up(show_id, _photo_data, _photo_ct)
                except Exception:
                    pass
            update_photo = remove_photo or _new_photo_url is not None
            await conn.execute(
                "UPDATE shows SET artist=$1, venue=$2, city=$3, date=$4, is_festival=$5, "
                "festival_name=$6, notes=$7, setlist=$8, support_acts=$9, artist_mbid=$10, "
                "artist_spotify_id=$11, artist_image_url=$12, artist_thumb_url=$13, artist_genres=$14, "
                "rating=$19, headliners=$20, "
                "photo_url = CASE WHEN $17 THEN $18 ELSE photo_url END "
                "WHERE id=$15 AND user_id=$16",
                artist, venue, city, date, is_festival, festival_name,
                notes, _json.dumps(setlist_data) if setlist_data else None,
                support_acts or None,
                mbid, spotify_id, image_url, thumb_url, genres, show_id, user["id"],
                update_photo, _new_photo_url if not remove_photo else None,
                rating, headliners or None,
            )

        if artist and date and not is_festival:
            norm_key = f"{artist.lower()}|{date}|{(venue or '').lower()}"
            event_id = await conn.fetchval(
                "INSERT INTO events (normalized_key, artist, date, venue, city) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (normalized_key) DO NOTHING RETURNING id",
                norm_key, artist, date, venue or None, city or None,
            )
            if event_id is None:
                event_id = await conn.fetchval(
                    "SELECT id FROM events WHERE normalized_key=$1", norm_key
                )
            if event_id and show_id:
                await conn.execute(
                    "UPDATE shows SET event_id=$1 WHERE id=$2 AND event_id IS DISTINCT FROM $1",
                    event_id, show_id,
                )

        await _upsert_artist(conn, artist, sp, now)
        for name, sp_result in zip(support_acts, support_sp_results):
            support_sp = sp_result if isinstance(sp_result, dict) else None
            await _upsert_artist(conn, name, support_sp, now)

                                  
        tag_ids_raw = str(form.get("tag_friend_ids", "")).strip()
        tag_uids: list[int] = []
        if tag_ids_raw:
            try:
                tag_uids = [int(v) for v in json.loads(tag_ids_raw) if str(v).strip()]
            except (ValueError, TypeError, json.JSONDecodeError):
                tag_uids = []
        if tag_uids and show_id is not None:
            actual_id = await conn.fetchval(
                "SELECT id FROM shows WHERE id = $1 AND user_id = $2", show_id, user["id"]
            )
            if actual_id:
                for tag_uid in tag_uids:
                    await conn.execute(
                        "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        show_id, tag_uid,
                    )
                await conn.execute(
                    "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    show_id, user["id"],
                )

from app.auth import verify_csrf
from app.jinja import render_mentions as _render_mentions

def _is_ajax(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch"

@router.post("/shows/{show_id}/like")
async def toggle_like(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    owner_id = None
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"]
        )
        if existing:
            await conn.execute("DELETE FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"])
            liked = False
        else:
            await conn.execute(
                "INSERT INTO show_likes (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                show_id, user["id"],
            )
            liked = True
            owner_id = await conn.fetchval("SELECT user_id FROM shows WHERE id = $1", show_id)
        count = await conn.fetchval("SELECT COUNT(*) FROM show_likes WHERE show_id = $1", show_id)
    if liked and owner_id:
        await create_notification(pool, user_id=owner_id, actor_id=user["id"], type="like", show_id=show_id)
    if _is_ajax(request):
        return JSONResponse({"liked": liked, "count": int(count)})
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)

@router.post("/shows/{show_id}/comments")
async def add_comment(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    body = str(form.get("body", "")).strip()[:500]
    comment_row = None
    owner_id = None
    comment_id = None
    if body:
        async with pool.acquire() as conn:
            show_row = await conn.fetchrow("SELECT id, user_id FROM shows WHERE id = $1", show_id)
            if show_row:
                now = int(time.time())
                comment_id = await conn.fetchval(
                    "INSERT INTO show_comments (show_id, user_id, body, created_at) VALUES ($1, $2, $3, $4) RETURNING id",
                    show_id, user["id"], body, now,
                )
                owner_id = show_row["user_id"]
                comment_row = {"id": comment_id, "body": _render_mentions(body), "created_at": now,
                               "username": user["username"], "avatar_url": user.get("avatar_url")}
    if owner_id and comment_id:
        await create_notification(pool, user_id=owner_id, actor_id=user["id"],
                                  type="comment", show_id=show_id, comment_id=comment_id)
    if _is_ajax(request):
        return JSONResponse(comment_row or {"error": "empty"})
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)

@router.post("/shows/{show_id}/comments/{comment_id}/delete")
async def delete_comment(
    show_id: int, comment_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM show_comments WHERE id = $1 AND user_id = $2", comment_id, user["id"]
        )
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)