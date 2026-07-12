import asyncio
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db import get_pool
from app.auth import flash, get_csrf_token, get_flashes, require_user, verify_csrf, optional_user
from app.jinja import templates
from app.routes.notifications import create_notification

router = APIRouter()


def _ctx(request: Request, user: dict, **kw) -> dict:
    return {"request": request, "user": user, "flashes": get_flashes(request), **kw}


@router.get("/profile", response_class=HTMLResponse)
async def own_profile(request: Request, user=Depends(require_user)):
    return RedirectResponse(f"/concert-tracker/u/{user['username']}", status_code=302)


@router.get("/profile/edit", response_class=HTMLResponse)
async def edit_profile_page(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        me = await conn.fetchrow(
            "SELECT id, username, bio, avatar_url, banner_url, accent_color, location, "
            "favorite_artists, social_links, pinned_show_id FROM users WHERE id = $1",
            user["id"],
        )
        user_shows = await conn.fetch(
            "SELECT id, artist, venue, city, date FROM shows WHERE user_id=$1 ORDER BY date DESC LIMIT 200",
            user["id"],
        )
    return templates.TemplateResponse(
        "profile_edit.html",
        _ctx(request, user, me=me, user_shows=user_shows, csrf=get_csrf_token(request)),
    )


@router.post("/profile/edit")
async def save_profile(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    import re
    await verify_csrf(request)
    form = await request.form()

    bio = str(form.get("bio", "")).strip()[:300] or None
    new_username = str(form.get("username", "")).strip()[:30]
    location = str(form.get("location", "")).strip()[:100] or None

    use_accent = "use_accent_color" in form
    raw_color = str(form.get("accent_color", "")).strip()
    accent_color = raw_color.lower() if use_accent and re.fullmatch(r"#[0-9a-fA-F]{6}", raw_color) else None

    fav_raw = str(form.get("favorite_artists", "")).strip()
    favorite_artists = [a.strip() for a in fav_raw.split(",") if a.strip()][:10] or None

    import re as _re
    _URL_RE = _re.compile(r'^https?://', _re.I)
    social_links: dict | None = {}
    for key in ("spotify", "lastfm", "instagram", "bandcamp", "soundcloud", "youtube", "twitter", "website"):
        val = str(form.get(f"social_{key}", "")).strip()[:200]
        if val:
            if not _URL_RE.match(val):
                val = "https://" + val
            social_links[key] = val
    social_links = social_links or None

    pinned_show_id = None
    raw_pin = str(form.get("pinned_show_id", "")).strip()
    if raw_pin.isdigit():
        pinned_show_id = int(raw_pin)

    if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", new_username):
        flash(request, "Username must be 2–30 characters: letters, numbers, underscores only.", "error")
        return RedirectResponse("/concert-tracker/profile/edit", status_code=302)

    remove_avatar = bool(form.get("remove_avatar"))
    avatar_url = None
    avatar_file = form.get("avatar")
    if not remove_avatar and avatar_file and hasattr(avatar_file, "filename") and avatar_file.filename:
        from app.r2 import upload_avatar
        data = await avatar_file.read()
        content_type = avatar_file.content_type or "application/octet-stream"
        try:
            avatar_url = await upload_avatar(user["id"], data, content_type)
        except ValueError as e:
            flash(request, str(e), "error")
            return RedirectResponse("/concert-tracker/profile/edit", status_code=302)

    remove_banner = bool(form.get("remove_banner"))
    banner_url = None
    banner_file = form.get("banner")
    if not remove_banner and banner_file and hasattr(banner_file, "filename") and banner_file.filename:
        from app.r2 import upload_banner
        data = await banner_file.read()
        content_type = banner_file.content_type or "application/octet-stream"
        try:
            banner_url = await upload_banner(user["id"], data, content_type)
        except ValueError as e:
            flash(request, str(e), "error")
            return RedirectResponse("/concert-tracker/profile/edit", status_code=302)
    update_banner = remove_banner or banner_url is not None

    async with pool.acquire() as conn:
        if new_username != user["username"]:
            existing = await conn.fetchval(
                "SELECT id FROM users WHERE username = $1 AND id != $2", new_username, user["id"]
            )
            if existing:
                flash(request, "That username is already taken.", "error")
                return RedirectResponse("/concert-tracker/profile/edit", status_code=302)
            await conn.execute(
                "UPDATE users SET prev_username = username, username = $1 WHERE id = $2",
                new_username, user["id"],
            )
            request.session["username"] = new_username
            user["username"] = new_username

        if pinned_show_id:
            ok = await conn.fetchval(
                "SELECT 1 FROM shows WHERE id=$1 AND user_id=$2", pinned_show_id, user["id"]
            )
            if not ok:
                pinned_show_id = None

        update_avatar = remove_avatar or avatar_url is not None
        await conn.execute(
            """UPDATE users SET
               bio = $1,
               avatar_url = CASE WHEN $2 THEN $3 ELSE avatar_url END,
               banner_url = CASE WHEN $4 THEN $5 ELSE banner_url END,
               accent_color = $6,
               location = $7,
               favorite_artists = $8,
               social_links = $9,
               pinned_show_id = $10
               WHERE id = $11""",
            bio, update_avatar, avatar_url, update_banner, banner_url,
            accent_color, location, favorite_artists, social_links, pinned_show_id,
            user["id"],
        )
        if remove_avatar:
            request.session["avatar_url"] = None
        elif avatar_url:
            request.session["avatar_url"] = avatar_url
        request.session["accent_color"] = accent_color

    flash(request, "Profile updated", "success")
    return RedirectResponse(f"/concert-tracker/u/{user['username']}", status_code=302)


@router.get("/social", response_class=HTMLResponse)
async def social_page(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    uid = user["id"]
    async with pool.acquire() as conn:
        feed, following_count, leaderboard_all, leaderboard_year, following, followers = await asyncio.gather(
            conn.fetch(
                "SELECT s.*, u.username, u.avatar_url AS user_avatar, "
                "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
                "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
                "FROM shows s "
                "JOIN follows f ON f.target_id = s.user_id "
                "JOIN users u ON u.id = s.user_id "
                "WHERE f.user_id = $1 ORDER BY s.created_at DESC LIMIT 20",
                uid,
            ),
            conn.fetchval("SELECT COUNT(*) FROM follows WHERE user_id=$1", uid),
            conn.fetch(
                "SELECT u.username, COUNT(s.id)::int AS count "
                "FROM users u JOIN shows s ON s.user_id = u.id "
                "GROUP BY u.id, u.username ORDER BY count DESC LIMIT 10",
            ),
            conn.fetch(
                "SELECT u.username, COUNT(s.id)::int AS count "
                "FROM users u JOIN shows s ON s.user_id = u.id "
                "WHERE EXTRACT(YEAR FROM s.date) = EXTRACT(YEAR FROM CURRENT_DATE) "
                "GROUP BY u.id, u.username ORDER BY count DESC LIMIT 10",
            ),
            conn.fetch(
                "SELECT u.id, u.username, u.avatar_url FROM follows f JOIN users u ON u.id = f.target_id "
                "WHERE f.user_id = $1", uid,
            ),
            conn.fetch(
                "SELECT u.id, u.username, u.avatar_url FROM follows f JOIN users u ON u.id = f.user_id "
                "WHERE f.target_id = $1", uid,
            ),
        )

    following_ids = {r["id"] for r in following}
    follower_ids = {r["id"] for r in followers}
    mutuals = [r for r in following if r["id"] in follower_ids]
    mutual_ids = [r["id"] for r in mutuals]

    shared_artists = []
    circle_shows = 0
    shared_cities = []
    if mutual_ids:
        async with pool.acquire() as conn:
            shared_artists, circle_shows_row, shared_cities = await asyncio.gather(
                conn.fetch(
                    "SELECT s.artist, COUNT(DISTINCT s2.user_id)::int AS mutual_count "
                    "FROM shows s "
                    "JOIN shows s2 ON s2.artist = s.artist AND s2.user_id = ANY($2) "
                    "WHERE s.user_id = $1 "
                    "GROUP BY s.artist ORDER BY mutual_count DESC, s.artist LIMIT 5",
                    uid, mutual_ids,
                ),
                conn.fetchrow(
                    "SELECT COALESCE(SUM(c),0)::int AS total FROM "
                    "(SELECT COUNT(*) AS c FROM shows WHERE user_id = ANY($1) GROUP BY user_id) sub",
                    mutual_ids + [uid],
                ),
                conn.fetch(
                    "SELECT s.city, COUNT(DISTINCT s2.user_id)::int AS mutual_count "
                    "FROM shows s "
                    "JOIN shows s2 ON s2.city = s.city AND s2.user_id = ANY($2) "
                    "WHERE s.user_id = $1 AND s.city IS NOT NULL AND s.city != '' "
                    "GROUP BY s.city ORDER BY mutual_count DESC LIMIT 3",
                    uid, mutual_ids,
                ),
            )
        circle_shows = circle_shows_row["total"] if circle_shows_row else 0

    feed_items: list = []
    seen_festival_keys: dict = {}
    for row in feed:
        fid = row["festival_id"] if row["is_festival"] and row["festival_name"] else None
        if fid:
            key = str(fid)
            if key not in seen_festival_keys:
                entry: dict = {
                    "type": "festival",
                    "festival_id": key,
                    "festival_name": row["festival_name"],
                    "city": row["city"],
                    "date": row["date"],
                    "username": row["username"],
                    "user_avatar": row["user_avatar"],
                    "like_count": 0,
                    "comment_count": 0,
                    "shows": [],
                }
                seen_festival_keys[key] = entry
                feed_items.append(entry)
            seen_festival_keys[key]["like_count"] += row["like_count"] or 0
            seen_festival_keys[key]["comment_count"] += row["comment_count"] or 0
            seen_festival_keys[key]["shows"].append(row)
        else:
            feed_items.append({"type": "show", "show": row})

    return templates.TemplateResponse(
        "social.html",
        _ctx(
            request,
            user,
            today=time.strftime("%Y-%m-%d"),
            feed=feed_items,
            feed_cursor=feed[-1]["created_at"] if feed else 0,
            has_more=len(feed) == 20,
            leaderboard_all=leaderboard_all,
            leaderboard_year=leaderboard_year,
            following=following,
            following_count=following_count,
            mutuals=mutuals,
            following_ids=following_ids,
            shared_artists=shared_artists,
            shared_cities=shared_cities,
            circle_shows=circle_shows,
            csrf=get_csrf_token(request),
        ),
    )


@router.get("/api/user-search")
async def user_search(q: str = "", pool=Depends(get_pool), user=Depends(require_user)):
    if len(q) < 1:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT username, avatar_url FROM users WHERE username ILIKE $1 AND id != $2 ORDER BY username LIMIT 8",
            f"{q}%", user["id"],
        )
    return [{"username": r["username"], "avatar_url": r["avatar_url"]} for r in rows]


def _is_ajax(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


@router.post("/u/follow")
async def follow(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    username = str(form.get("follow_user", "")).strip()

    target_id = None
    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
        if target and target["id"] != user["id"]:
            result = await conn.execute(
                "INSERT INTO follows (user_id, target_id, created_at) VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                user["id"], target["id"], int(time.time()),
            )
            if result != "INSERT 0 0":
                target_id = target["id"]

    if target_id:
        await create_notification(pool, user_id=target_id, actor_id=user["id"], type="follow")

    if _is_ajax(request):
        return JSONResponse({"following": True, "username": username})
    return RedirectResponse(f"/concert-tracker/u/{username}", status_code=302)


@router.post("/u/unfollow")
async def unfollow(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()

    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
        if target:
            await conn.execute(
                "DELETE FROM follows WHERE user_id = $1 AND target_id = $2",
                user["id"], target["id"],
            )

    if _is_ajax(request):
        return JSONResponse({"following": False, "username": username})
    return RedirectResponse(f"/concert-tracker/u/{username}", status_code=302)


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(request: Request, pool=Depends(get_pool), user=Depends(require_user), q: str = "", page: int = 1):
    limit = 24
    offset = (page - 1) * limit
    async with pool.acquire() as conn:
        if q:
            rows = await conn.fetch(
                "SELECT u.id, u.username, u.avatar_url, u.bio, "
                "(SELECT COUNT(*) FROM shows WHERE user_id=u.id)::int AS show_count, "
                "(SELECT COUNT(*) FROM follows WHERE target_id=u.id)::int AS follower_count "
                "FROM users u WHERE u.username ILIKE $1 AND u.id != $2 "
                "ORDER BY u.username LIMIT $3 OFFSET $4",
                f"%{q}%", user["id"], limit, offset,
            )
            total = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE username ILIKE $1 AND id != $2", f"%{q}%", user["id"]
            )
        else:
            rows = await conn.fetch(
                "SELECT u.id, u.username, u.avatar_url, u.bio, "
                "(SELECT COUNT(*) FROM shows WHERE user_id=u.id)::int AS show_count, "
                "(SELECT COUNT(*) FROM follows WHERE target_id=u.id)::int AS follower_count "
                "FROM users u WHERE u.id != $1 "
                "ORDER BY show_count DESC, u.username LIMIT $2 OFFSET $3",
                user["id"], limit, offset,
            )
            total = await conn.fetchval("SELECT COUNT(*) FROM users WHERE id != $1", user["id"])
        i_follow = await conn.fetch("SELECT target_id FROM follows WHERE user_id=$1", user["id"])
    following_ids = {r["target_id"] for r in i_follow}
    pages = (total + limit - 1) // limit
    return templates.TemplateResponse(
        "discover.html",
        _ctx(request, user, rows=rows, following_ids=following_ids,
             q=q, page=page, pages=pages, csrf=get_csrf_token(request)),
    )


@router.get("/api/feed")
async def feed_api(request: Request, pool=Depends(get_pool), user=Depends(require_user),
                   before: int = 0, limit: int = 20):
    limit = min(limit, 40)
    uid = user["id"]
    async with pool.acquire() as conn:
        sql = (
            "SELECT s.*, u.username, u.avatar_url AS user_avatar, "
            "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
            "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
            "FROM shows s "
            "JOIN follows f ON f.target_id = s.user_id "
            "JOIN users u ON u.id = s.user_id "
            "WHERE f.user_id = $1"
        )
        params: list = [uid]
        if before:
            params.append(before)
            sql += f" AND s.created_at < ${len(params)}"
        sql += f" ORDER BY s.created_at DESC LIMIT ${len(params)+1}"
        params.append(limit)
        rows = await conn.fetch(sql, *params)
    def _ser(r):
        d = dict(r)
        if d.get("date") is not None:
            d["date"] = str(d["date"])
        return d

    feed_items: list = []
    seen: dict = {}
    for row in rows:
        d = _ser(row)
        fid = d["festival_id"] if d.get("is_festival") and d.get("festival_name") else None
        if fid:
            key = str(fid)
            if key not in seen:
                entry: dict = {
                    "type": "festival",
                    "festival_id": key,
                    "festival_name": d["festival_name"],
                    "city": d["city"],
                    "date": d["date"],
                    "username": d["username"],
                    "user_avatar": d["user_avatar"],
                    "like_count": 0,
                    "comment_count": 0,
                    "shows": [],
                }
                seen[key] = entry
                feed_items.append(entry)
            seen[key]["like_count"] += d.get("like_count") or 0
            seen[key]["comment_count"] += d.get("comment_count") or 0
            seen[key]["shows"].append(d)
        else:
            feed_items.append({"type": "show", "show": d})

    return JSONResponse({"items": feed_items, "has_more": len(rows) == limit})


@router.get("/u/{username}/followers", response_class=HTMLResponse)
async def followers_page(username: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        profile = await conn.fetchrow("SELECT id, username, avatar_url FROM users WHERE username = $1", username)
        if not profile:
            flash(request, "User not found", "error")
            return RedirectResponse("/concert-tracker/social", status_code=302)
        rows = await conn.fetch(
            "SELECT u.id, u.username, u.avatar_url FROM follows f JOIN users u ON u.id = f.user_id "
            "WHERE f.target_id = $1 ORDER BY u.username", profile["id"],
        )
        i_follow = await conn.fetch("SELECT target_id FROM follows WHERE user_id = $1", user["id"])
        they_follow_me = await conn.fetch("SELECT user_id FROM follows WHERE target_id = $1", user["id"])
    following_ids = {r["target_id"] for r in i_follow}
    mutual_ids = following_ids & {r["user_id"] for r in they_follow_me}
    return templates.TemplateResponse("follow_list.html", _ctx(
        request, user, profile=profile, rows=rows, following_ids=following_ids,
        mutual_ids=mutual_ids, list_type="followers", csrf=get_csrf_token(request),
    ))


@router.get("/u/{username}/following", response_class=HTMLResponse)
async def following_page(username: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        profile = await conn.fetchrow("SELECT id, username, avatar_url FROM users WHERE username = $1", username)
        if not profile:
            flash(request, "User not found", "error")
            return RedirectResponse("/concert-tracker/social", status_code=302)
        rows = await conn.fetch(
            "SELECT u.id, u.username, u.avatar_url FROM follows f JOIN users u ON u.id = f.target_id "
            "WHERE f.user_id = $1 ORDER BY u.username", profile["id"],
        )
        i_follow = await conn.fetch("SELECT target_id FROM follows WHERE user_id = $1", user["id"])
        they_follow_me = await conn.fetch("SELECT user_id FROM follows WHERE target_id = $1", user["id"])
    following_ids = {r["target_id"] for r in i_follow}
    mutual_ids = following_ids & {r["user_id"] for r in they_follow_me}
    return templates.TemplateResponse("follow_list.html", _ctx(
        request, user, profile=profile, rows=rows, following_ids=following_ids,
        mutual_ids=mutual_ids, list_type="following", csrf=get_csrf_token(request),
    ))


async def _none():
    return None


async def _empty():
    return []


def _group_festivals(rows) -> list:
    seen: dict = {}
    items: list = []
    for row in rows:
        fid = row["festival_id"] if row["is_festival"] and row["festival_name"] else None
        if fid:
            key = str(fid)
            if key not in seen:
                seen[key] = {
                    "type": "festival",
                    "festival_id": key,
                    "festival_name": row["festival_name"],
                    "city": row["city"],
                    "date": row["date"],
                    "like_count": 0,
                    "comment_count": 0,
                    "shows": [],
                }
                items.append(seen[key])
            seen[key]["like_count"] += row["like_count"] or 0
            seen[key]["comment_count"] += row["comment_count"] or 0
            seen[key]["shows"].append(row)
        else:
            items.append({"type": "show", "show": row})
    return items


@router.get("/u/{username}", response_class=HTMLResponse)
async def friend_profile(
    username: str,
    request: Request,
    pool=Depends(get_pool),
    user=Depends(optional_user),
):
    async with pool.acquire() as conn:
        profile = await conn.fetchrow(
            "SELECT id, username, bio, avatar_url, created_at, "
            "banner_url, accent_color, location, favorite_artists, social_links, pinned_show_id "
            "FROM users WHERE username = $1",
            username,
        )
        if not profile:
            if user:
                flash(request, "User not found", "error")
            return RedirectResponse("/concert-tracker/social" if user else "/concert-tracker/login", status_code=302)

        pid = profile["id"]
        uid = user["id"] if user else None
        fav_names = list(profile["favorite_artists"] or [])

        (
            recent_rows, is_following, is_follower, per_year, top_artists,
            fav_thumb_rows, top_venues, shared, show_count, follower_count,
            following_count, pinned_show, profile_lists,
        ) = await asyncio.gather(
            conn.fetch(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
                "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
                "FROM shows s WHERE s.user_id = $1 ORDER BY s.created_at DESC LIMIT 20",
                pid,
            ),
            conn.fetchval("SELECT 1 FROM follows WHERE user_id=$1 AND target_id=$2", uid, pid) if uid else _none(),
            conn.fetchval("SELECT 1 FROM follows WHERE user_id=$1 AND target_id=$2", pid, uid) if uid else _none(),
            conn.fetch(
                "SELECT EXTRACT(YEAR FROM date)::int AS year, COUNT(*)::int AS count "
                "FROM shows WHERE user_id=$1 GROUP BY year ORDER BY year",
                pid,
            ),
            conn.fetch(
                "SELECT s.artist, COUNT(*)::int AS count, "
                "COALESCE(MAX(a.thumb_url), MAX(s.artist_thumb_url)) AS thumb_url "
                "FROM shows s LEFT JOIN artists a ON LOWER(a.name) = LOWER(s.artist) "
                "WHERE s.user_id=$1 GROUP BY s.artist ORDER BY count DESC LIMIT 5",
                pid,
            ),
            conn.fetch("SELECT name, thumb_url FROM artists WHERE name = ANY($1)", fav_names) if fav_names else _empty(),
            conn.fetch(
                "SELECT venue, COUNT(*)::int AS count FROM shows WHERE user_id=$1 "
                "GROUP BY venue ORDER BY count DESC LIMIT 5",
                pid,
            ),
            conn.fetch(
                "SELECT s.artist, s.date, s.venue, s.city, s.artist_thumb_url, s.is_festival, s.festival_name "
                "FROM shows s WHERE s.user_id = $1 AND EXISTS ("
                "  SELECT 1 FROM shows s2 WHERE s2.user_id = $2 "
                "  AND s2.artist = s.artist AND s2.date = s.date"
                ") ORDER BY s.date DESC",
                uid, pid,
            ) if uid else _empty(),
            conn.fetchval(
                "SELECT (SELECT COUNT(*) FROM shows WHERE user_id=$1 AND (is_festival = FALSE OR festival_name IS NULL))"
                " + (SELECT COUNT(DISTINCT festival_name) FROM shows WHERE user_id=$1 AND is_festival = TRUE AND festival_name IS NOT NULL)",
                pid,
            ),
            conn.fetchval("SELECT COUNT(*) FROM follows WHERE target_id=$1", pid),
            conn.fetchval("SELECT COUNT(*) FROM follows WHERE user_id=$1", pid),
            conn.fetchrow(
                "SELECT id, artist, venue, city, date, artist_thumb_url, is_festival, festival_name "
                "FROM shows WHERE id=$1",
                profile["pinned_show_id"],
            ) if profile["pinned_show_id"] else _none(),
            conn.fetch(
                "SELECT l.id, l.title, l.is_ranked, COUNT(li.id)::int AS item_count "
                "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
                "WHERE l.user_id = $1 GROUP BY l.id ORDER BY l.updated_at DESC LIMIT 6",
                pid,
            ),
        )

    fav_thumbs = {r["name"]: r["thumb_url"] for r in fav_thumb_rows}

    return templates.TemplateResponse(
        "profile.html",
        _ctx(
            request,
            user,
            profile=profile,
            items=_group_festivals(recent_rows),
            today=time.strftime("%Y-%m-%d"),
            show_count=show_count,
            follower_count=follower_count,
            following_count=following_count,
            is_following=bool(is_following),
            is_follower=bool(is_follower),
            is_mutual=bool(is_following and is_follower),
            is_own_profile=uid is not None and uid == pid,
            per_year=[dict(r) for r in per_year],
            top_artists=top_artists,
            top_venues=top_venues,
            shared=shared,
            pinned_show=pinned_show,
            profile_lists=list(profile_lists),
            social_links=dict(profile["social_links"] or {}),
            favorite_artists=[{"name": n, "thumb_url": fav_thumbs.get(n)} for n in fav_names],
            csrf=get_csrf_token(request) if user else "",
        ),
    )


@router.get("/u/{username}/tab/{tab}", response_class=HTMLResponse)
async def profile_tab(
    username: str,
    tab: str,
    request: Request,
    pool=Depends(get_pool),
    user=Depends(optional_user),
    year: str = "",
    artist_filter: str = "",
    kind: str = "",
    sort: str = "date_desc",
):

    if tab not in ("recent", "top", "all", "stats"):
        return RedirectResponse(f"/concert-tracker/u/{username}", status_code=302)

    async with pool.acquire() as conn:
        profile = await conn.fetchrow(
            "SELECT id, username FROM users WHERE username = $1", username
        )
        if not profile:
            return RedirectResponse(
                "/concert-tracker/social" if user else "/concert-tracker/login",
                status_code=302,
            )
        pid = profile["id"]
        today = time.strftime("%Y-%m-%d")

        if tab == "recent":
            rows = await conn.fetch(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
                "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
                "FROM shows s WHERE s.user_id = $1 ORDER BY s.created_at DESC LIMIT 20",
                pid,
            )
            return templates.TemplateResponse(
                "profile_tab_recent.html",
                _ctx(request, user, items=_group_festivals(rows), today=today),
            )

        if tab == "top":
            rows = await conn.fetch(
                "SELECT s.*, "
                "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
                "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
                "FROM shows s WHERE s.user_id = $1 AND s.rating IS NOT NULL "
                "ORDER BY s.rating DESC, s.date DESC",
                pid,
            )
            return templates.TemplateResponse(
                "profile_tab_top.html",
                _ctx(request, user, shows=list(rows), today=today),
            )

        if tab == "all":
            order = {
                "date_desc": "s.date DESC",
                "date_asc": "s.date ASC",
                "artist": "s.artist ASC",
                "venue": "s.venue ASC",
                "rating_desc": "s.rating DESC NULLS LAST",
            }.get(sort, "s.date DESC")
            clauses = ["s.user_id = $1"]
            params: list = [pid]
            if year:
                params.append(int(year))
                clauses.append(f"EXTRACT(YEAR FROM s.date) = ${len(params)}")
            if artist_filter:
                params.append(f"%{artist_filter.lower()}%")
                clauses.append(f"LOWER(s.artist) LIKE ${len(params)}")
            if kind == "festival":
                clauses.append("s.is_festival = TRUE")
            elif kind == "standalone":
                clauses.append("s.is_festival = FALSE")
            where = " AND ".join(clauses)
            rows, year_rows = await asyncio.gather(
                conn.fetch(
                    f"SELECT s.*, "
                    "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
                    "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
                    f"FROM shows s WHERE {where} ORDER BY {order}",
                    *params,
                ),
                conn.fetch(
                    "SELECT DISTINCT EXTRACT(YEAR FROM date)::int AS y FROM shows WHERE user_id = $1 ORDER BY y DESC",
                    pid,
                ),
            )
            return templates.TemplateResponse(
                "profile_tab_all.html",
                _ctx(
                    request, user,
                    items=_group_festivals(rows),
                    today=today,
                    profile_username=profile["username"],
                    years=[r["y"] for r in year_rows],
                    filters={"year": year, "artist": artist_filter, "kind": kind, "sort": sort},
                ),
            )

        # tab == "stats"
        summary, rating_dist, per_month, top_genres = await asyncio.gather(
            conn.fetchrow(
                "SELECT COUNT(*)::int AS total, "
                "COUNT(DISTINCT LOWER(artist))::int AS artists, "
                "COUNT(DISTINCT LOWER(venue))::int AS venues, "
                "COUNT(DISTINCT LOWER(city))::int AS cities, "
                "COUNT(rating)::int AS rated, "
                "ROUND(AVG(rating)::numeric, 1) AS avg_rating, "
                "MIN(EXTRACT(YEAR FROM date)::int) AS first_year, "
                "MAX(EXTRACT(YEAR FROM date)::int) AS last_year "
                "FROM shows WHERE user_id = $1",
                pid,
            ),
            conn.fetch(
                "SELECT (FLOOR(rating * 2) / 2)::float AS half_star, COUNT(*)::int AS cnt "
                "FROM shows WHERE user_id = $1 AND rating IS NOT NULL "
                "GROUP BY half_star ORDER BY half_star DESC",
                pid,
            ),
            conn.fetch(
                "SELECT EXTRACT(MONTH FROM date)::int AS month, COUNT(*)::int AS cnt "
                "FROM shows WHERE user_id = $1 GROUP BY month ORDER BY month",
                pid,
            ),
            conn.fetch(
                "SELECT unnest(artist_genres) AS genre, COUNT(*)::int AS cnt "
                "FROM shows WHERE user_id = $1 AND artist_genres IS NOT NULL "
                "GROUP BY genre ORDER BY cnt DESC LIMIT 8",
                pid,
            ),
        )
        return templates.TemplateResponse(
            "profile_tab_stats.html",
            _ctx(
                request, user,
                summary=dict(summary) if summary else {},
                rating_dist=[{"half_star": float(r["half_star"]), "cnt": r["cnt"]} for r in rating_dist],
                per_month=[dict(r) for r in per_month],
                top_genres=[dict(r) for r in top_genres],
            ),
        )
