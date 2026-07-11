import os
import time
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_pool
from app.auth import flash, get_csrf_token, get_flashes, require_user
from app.jinja import templates
import app.setlistfm as setlistfm
import app.spotify as spotify
import app.musicbrainz as musicbrainz
import app.ticketmaster as ticketmaster

_GOOGLE_PLACES_KEY = os.environ.get("GOOGLE_PLACES_KEY", "")

router = APIRouter()


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
):
    order = {
        "date_desc": "date DESC",
        "date_asc": "date ASC",
        "artist": "artist ASC",
        "venue": "venue ASC",
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
    sql = f"SELECT * FROM shows WHERE {where} ORDER BY {order}"

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        years = await conn.fetch(
            "SELECT DISTINCT EXTRACT(YEAR FROM date)::int AS y FROM shows WHERE user_id = $1 ORDER BY y DESC",
            user["id"],
        )

    seen_festivals: dict = {}
    items: list = []
    for row in rows:
        show = _parse_show(row)
        if show["is_festival"] and show.get("festival_name"):
            fname = show["festival_name"]
            if fname not in seen_festivals:
                entry: dict = {
                    "type": "festival",
                    "festival_name": fname,
                    "city": show["city"],
                    "date": show["date"],
                    "shows": [],
                }
                seen_festivals[fname] = entry
                items.append(entry)
            seen_festivals[fname]["shows"].append(show)
        else:
            items.append({"type": "show", "show": show})

    return templates.TemplateResponse(
        "list.html",
        _ctx(
            request,
            user,
            items=items,
            years=[r["y"] for r in years],
            filters={"year": year, "artist": artist, "kind": kind, "sort": sort},
            today=time.strftime("%Y-%m-%d"),
            csrf=get_csrf_token(request),
        ),
    )


@router.get("/shows/festival/{festival_name}", response_class=HTMLResponse)
async def festival_detail(
    festival_name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT s.*, u.username AS owner_username, u.avatar_url AS owner_avatar "
            "FROM shows s JOIN users u ON u.id = s.user_id "
            "WHERE s.user_id = $1 AND s.festival_name = $2 ORDER BY s.date ASC, s.artist ASC",
            user["id"], festival_name,
        )
        if not rows:
            flash(request, "Festival not found", "error")
            return RedirectResponse("/concert-tracker/shows", status_code=302)
        show_ids = [r["id"] for r in rows]
        rep_id = rows[0]["id"]
        attendees = await conn.fetch(
            "SELECT DISTINCT u.id, u.username, u.avatar_url FROM show_attendees sa "
            "JOIN users u ON u.id = sa.user_id WHERE sa.show_id = ANY($1)",
            show_ids,
        )
        already_tagged = {a["id"] for a in attendees}
        following = await conn.fetch(
            "SELECT u.id, u.username FROM follows f JOIN users u ON u.id = f.target_id "
            "WHERE f.user_id = $1",
            user["id"],
        )
        taggable = [f for f in following if f["id"] not in already_tagged]
        like_count = await conn.fetchval("SELECT COUNT(*) FROM show_likes WHERE show_id = $1", rep_id)
        user_liked = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", rep_id, user["id"]
        )
        comments = await conn.fetch(
            "SELECT c.id, c.body, c.created_at, u.username, u.avatar_url "
            "FROM show_comments c JOIN users u ON u.id = c.user_id "
            "WHERE c.show_id = $1 ORDER BY c.created_at ASC",
            rep_id,
        )
    shows = [_parse_show(r) for r in rows]
    return templates.TemplateResponse(
        "festival_detail.html",
        _ctx(
            request, user,
            festival_name=festival_name,
            city=shows[0]["city"],
            shows=shows,
            attendees=attendees,
            taggable=taggable,
            rep_id=rep_id,
            like_count=like_count,
            user_liked=bool(user_liked),
            comments=list(comments),
            csrf=get_csrf_token(request),
        ),
    )


@router.post("/shows/festival/{festival_name}/like")
async def festival_like(
    festival_name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM shows WHERE user_id = $1 AND festival_name = $2 ORDER BY date ASC, artist ASC LIMIT 1",
            user["id"], festival_name,
        )
        if not row:
            return RedirectResponse("/concert-tracker/shows", status_code=302)
        show_id = row["id"]
        existing = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"]
        )
        if existing:
            await conn.execute("DELETE FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"])
        else:
            await conn.execute(
                "INSERT INTO show_likes (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                show_id, user["id"],
            )
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)


@router.post("/shows/festival/{festival_name}/comments")
async def festival_add_comment(
    festival_name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    form = await request.form()
    body = str(form.get("body", "")).strip()[:500]
    if body:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM shows WHERE user_id = $1 AND festival_name = $2 ORDER BY date ASC, artist ASC LIMIT 1",
                user["id"], festival_name,
            )
            if row:
                await conn.execute(
                    "INSERT INTO show_comments (show_id, user_id, body, created_at) VALUES ($1, $2, $3, $4)",
                    row["id"], user["id"], body, int(time.time()),
                )
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)


@router.post("/shows/festival/{festival_name}/comments/{comment_id}/delete")
async def festival_delete_comment(
    festival_name: str, comment_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM show_comments WHERE id = $1 AND user_id = $2", comment_id, user["id"]
        )
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)


@router.post("/shows/festival/{festival_name}/tag")
async def tag_festival_friend(
    festival_name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()
    friend_id = int(form.get("friend_id", 0))
    if not friend_id:
        return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM shows WHERE user_id = $1 AND festival_name = $2",
            user["id"], festival_name,
        )
        for row in rows:
            await conn.execute(
                "INSERT INTO show_attendees (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                row["id"], friend_id,
            )
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)


@router.post("/shows/festival/{festival_name}/untag")
async def untag_festival_friend(
    festival_name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()
    friend_id = int(form.get("friend_id", 0))
    if not friend_id:
        return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id FROM shows WHERE user_id = $1 AND festival_name = $2",
            user["id"], festival_name,
        )
        show_ids = [r["id"] for r in rows]
        await conn.execute(
            "DELETE FROM show_attendees WHERE show_id = ANY($1) AND user_id = $2",
            show_ids, friend_id,
        )
    return RedirectResponse(f"/concert-tracker/shows/festival/{festival_name}", status_code=302)


@router.post("/shows/festival/{festival_name}/delete")
async def delete_festival(
    festival_name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    from app.auth import verify_csrf
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM shows WHERE user_id = $1 AND festival_name = $2",
            user["id"], festival_name,
        )
    flash(request, f"{festival_name} deleted", "info")
    return RedirectResponse("/concert-tracker/shows", status_code=302)


@router.get("/shows/add-festival", response_class=HTMLResponse)
async def add_festival_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(
        "festival_form.html",
        _ctx(request, user, csrf=get_csrf_token(request)),
    )


@router.post("/shows/add-festival")
async def add_festival(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()

    import json as _json
    import datetime

    festival_name = str(form.get("festival_name", "")).strip()[:200]
    venue = festival_name  # for festivals the name IS the venue
    city = str(form.get("city", "")).strip()[:200]
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

    now = int(time.time())

    import asyncio
    sp_results, sl_results = await asyncio.gather(
        asyncio.gather(*[spotify.search_artist(a["name"]) for a in selected], return_exceptions=True),
        asyncio.gather(*[setlistfm.search(a["name"], str(a["date"])) for a in selected], return_exceptions=True),
    )

    async with pool.acquire() as conn:
        for artist_data, sp, sl in zip(selected, sp_results, sl_results):
            sp = sp if isinstance(sp, dict) else None
            sl = sl if isinstance(sl, dict) else None
            artist_name = str(artist_data["name"])[:200]
            date = datetime.date.fromisoformat(str(artist_data["date"]))
            setlist_data = {"songs": sl["songs"], "url": sl.get("url"), "id": sl.get("id")} if sl and sl.get("songs") else None
            await conn.execute(
                "INSERT INTO shows (user_id, artist, venue, city, date, is_festival, festival_name, "
                "artist_spotify_id, artist_image_url, artist_thumb_url, artist_genres, setlist, created_at) "
                "VALUES ($1,$2,$3,$4,$5,TRUE,$6,$7,$8,$9,$10,$11,$12)",
                user["id"], artist_name, venue, city, date, festival_name,
                sp["id"] if sp else None,
                sp["image_url"] if sp else None,
                sp["thumb_url"] if sp else None,
                sp["genres"] if sp else [],
                json.dumps(setlist_data) if setlist_data else None,
                now,
            )
            await _upsert_artist(conn, artist_name, sp, now)

    flash(request, f"Logged {len(selected)} set{'s' if len(selected) != 1 else ''}!", "success")
    return RedirectResponse("/concert-tracker/shows", status_code=302)


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


@router.get("/shows/{show_id}", response_class=HTMLResponse)
async def show_detail(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        show = await conn.fetchrow(
            "SELECT s.*, u.username AS owner_username, u.avatar_url AS owner_avatar "
            "FROM shows s JOIN users u ON u.id = s.user_id WHERE s.id = $1",
            show_id,
        )
        if not show:
            flash(request, "Show not found", "error")
            return RedirectResponse("/concert-tracker/social", status_code=302)
        show = _parse_show(show)
        is_owner = show["user_id"] == user["id"]
        attendees = await conn.fetch(
            "SELECT u.username FROM show_attendees sa JOIN users u ON u.id = sa.user_id "
            "WHERE sa.show_id = $1 AND sa.user_id <> $2",
            show_id, show["user_id"],
        )
        like_count = await conn.fetchval("SELECT COUNT(*) FROM show_likes WHERE show_id = $1", show_id)
        user_liked = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"]
        )
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
             csrf=get_csrf_token(request)),
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
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)


@router.post("/shows/{show_id}/delete")
async def delete_show(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM shows WHERE id = $1 AND user_id = $2", show_id, user["id"])
    flash(request, "Show deleted", "info")
    return RedirectResponse("/concert-tracker/shows", status_code=302)


@router.post("/shows/{show_id}/tag")
async def tag_friend(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    friend_id = int(form.get("friend_id", 0))
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
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)


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


# ─── internal helpers ────────────────────────────────────────────────────────

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
    city = str(form.get("city", "")).strip()[:200]
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

    # Enrich artist data asynchronously
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

    # Spotify lookups for support acts (parallel with each other)
    support_sp_results = await asyncio.gather(
        *[spotify.search_artist(name) for name in support_acts],
        return_exceptions=True,
    ) if support_acts else []

    async with pool.acquire() as conn:
        if show_id is None:
            await conn.execute(
                "INSERT INTO shows (user_id, artist, venue, city, date, is_festival, festival_name, "
                "notes, setlist, support_acts, artist_mbid, artist_spotify_id, artist_image_url, "
                "artist_thumb_url, artist_genres, created_at) VALUES "
                "($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)",
                user["id"], artist, venue, city, date, is_festival, festival_name,
                notes, _json.dumps(setlist_data) if setlist_data else None,
                support_acts or None,
                mbid, spotify_id, image_url, thumb_url, genres, now,
            )
        else:
            await conn.execute(
                "UPDATE shows SET artist=$1, venue=$2, city=$3, date=$4, is_festival=$5, "
                "festival_name=$6, notes=$7, setlist=$8, support_acts=$9, artist_mbid=$10, "
                "artist_spotify_id=$11, artist_image_url=$12, artist_thumb_url=$13, artist_genres=$14 "
                "WHERE id=$15 AND user_id=$16",
                artist, venue, city, date, is_festival, festival_name,
                notes, _json.dumps(setlist_data) if setlist_data else None,
                support_acts or None,
                mbid, spotify_id, image_url, thumb_url, genres, show_id, user["id"],
            )

        # Persist headliner and support acts to the global artist catalogue
        await _upsert_artist(conn, artist, sp, now)
        for name, sp_result in zip(support_acts, support_sp_results):
            support_sp = sp_result if isinstance(sp_result, dict) else None
            await _upsert_artist(conn, name, support_sp, now)


from app.auth import verify_csrf


@router.post("/shows/{show_id}/like")
async def toggle_like(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        existing = await conn.fetchval(
            "SELECT 1 FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"]
        )
        if existing:
            await conn.execute("DELETE FROM show_likes WHERE show_id = $1 AND user_id = $2", show_id, user["id"])
        else:
            await conn.execute(
                "INSERT INTO show_likes (show_id, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                show_id, user["id"],
            )
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)


@router.post("/shows/{show_id}/comments")
async def add_comment(show_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    body = str(form.get("body", "")).strip()[:500]
    if body:
        async with pool.acquire() as conn:
            exists = await conn.fetchval("SELECT 1 FROM shows WHERE id = $1", show_id)
            if exists:
                await conn.execute(
                    "INSERT INTO show_comments (show_id, user_id, body, created_at) VALUES ($1, $2, $3, $4)",
                    show_id, user["id"], body, int(time.time()),
                )
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
    return RedirectResponse(f"/concert-tracker/shows/{show_id}", status_code=302)