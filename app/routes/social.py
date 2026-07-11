import asyncio
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_pool
from app.auth import flash, get_csrf_token, get_flashes, require_user, verify_csrf
from app.jinja import templates

router = APIRouter()


def _ctx(request: Request, user: dict, **kw) -> dict:
    return {"request": request, "user": user, "flashes": get_flashes(request), **kw}


@router.get("/profile", response_class=HTMLResponse)
async def own_profile(request: Request, user=Depends(require_user)):
    return RedirectResponse(f"/concert-tracker/u/{user['username']}", status_code=302)


@router.get("/profile/edit", response_class=HTMLResponse)
async def edit_profile_page(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        me = await conn.fetchrow("SELECT id, username, bio, avatar_url FROM users WHERE id = $1", user["id"])
    return templates.TemplateResponse(
        "profile_edit.html",
        _ctx(request, user, me=me, csrf=get_csrf_token(request)),
    )


@router.post("/profile/edit")
async def save_profile(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    import re
    await verify_csrf(request)
    form = await request.form()
    bio = str(form.get("bio", "")).strip()[:300] or None
    new_username = str(form.get("username", "")).strip()[:30]

    if not re.fullmatch(r"[A-Za-z0-9_]{2,30}", new_username):
        flash(request, "Username must be 2–30 characters: letters, numbers, underscores only.", "error")
        return RedirectResponse("/concert-tracker/profile/edit", status_code=302)

    avatar_url = None
    avatar_file = form.get("avatar")
    if avatar_file and hasattr(avatar_file, "filename") and avatar_file.filename:
        from app.r2 import upload_avatar
        data = await avatar_file.read()
        content_type = avatar_file.content_type or "application/octet-stream"
        try:
            avatar_url = await upload_avatar(user["id"], data, content_type)
        except ValueError as e:
            flash(request, str(e), "error")
            return RedirectResponse("/concert-tracker/profile/edit", status_code=302)

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

        if avatar_url:
            await conn.execute(
                "UPDATE users SET bio = $1, avatar_url = $2 WHERE id = $3",
                bio, avatar_url, user["id"],
            )
            request.session["avatar_url"] = avatar_url
        else:
            await conn.execute("UPDATE users SET bio = $1 WHERE id = $2", bio, user["id"])

    flash(request, "Profile updated", "success")
    return RedirectResponse(f"/concert-tracker/u/{user['username']}", status_code=302)


@router.get("/social", response_class=HTMLResponse)
async def social_page(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    uid = user["id"]
    async with pool.acquire() as conn:
        feed = await conn.fetch(
            "SELECT s.*, u.username, u.avatar_url AS user_avatar, "
            "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
            "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
            "FROM shows s "
            "JOIN follows f ON f.target_id = s.user_id "
            "JOIN users u ON u.id = s.user_id "
            "WHERE f.user_id = $1 ORDER BY s.created_at DESC LIMIT 40",
            uid,
        )
        leaderboard_all = await conn.fetch(
            "SELECT u.username, COUNT(s.id)::int AS count "
            "FROM users u JOIN shows s ON s.user_id = u.id "
            "GROUP BY u.id, u.username ORDER BY count DESC LIMIT 10",
        )
        leaderboard_year = await conn.fetch(
            "SELECT u.username, COUNT(s.id)::int AS count "
            "FROM users u JOIN shows s ON s.user_id = u.id "
            "WHERE EXTRACT(YEAR FROM s.date) = EXTRACT(YEAR FROM CURRENT_DATE) "
            "GROUP BY u.id, u.username ORDER BY count DESC LIMIT 10",
        )
        following = await conn.fetch(
            "SELECT u.id, u.username, u.avatar_url FROM follows f JOIN users u ON u.id = f.target_id "
            "WHERE f.user_id = $1", uid,
        )
        followers = await conn.fetch(
            "SELECT u.id, u.username, u.avatar_url FROM follows f JOIN users u ON u.id = f.user_id "
            "WHERE f.target_id = $1", uid,
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
            shared_artists = await conn.fetch(
                "SELECT s.artist, COUNT(DISTINCT s2.user_id)::int AS mutual_count "
                "FROM shows s "
                "JOIN shows s2 ON s2.artist = s.artist AND s2.user_id = ANY($2) "
                "WHERE s.user_id = $1 "
                "GROUP BY s.artist ORDER BY mutual_count DESC, s.artist LIMIT 5",
                uid, mutual_ids,
            )
            circle_shows_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(c),0)::int AS total FROM "
                "(SELECT COUNT(*) AS c FROM shows WHERE user_id = ANY($1) GROUP BY user_id) sub",
                mutual_ids + [uid],
            )
            shared_cities = await conn.fetch(
                "SELECT s.city, COUNT(DISTINCT s2.user_id)::int AS mutual_count "
                "FROM shows s "
                "JOIN shows s2 ON s2.city = s.city AND s2.user_id = ANY($2) "
                "WHERE s.user_id = $1 AND s.city IS NOT NULL AND s.city != '' "
                "GROUP BY s.city ORDER BY mutual_count DESC LIMIT 3",
                uid, mutual_ids,
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
                    "shows": [],
                }
                seen_festival_keys[key] = entry
                feed_items.append(entry)
            seen_festival_keys[key]["shows"].append(row)
        else:
            feed_items.append({"type": "show", "show": row})

    return templates.TemplateResponse(
        "social.html",
        _ctx(
            request,
            user,
            feed=feed_items,
            leaderboard_all=leaderboard_all,
            leaderboard_year=leaderboard_year,
            following=following,
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


@router.post("/u/follow")
async def follow(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    username = str(form.get("follow_user", "")).strip()

    async with pool.acquire() as conn:
        target = await conn.fetchrow("SELECT id FROM users WHERE username = $1", username)
        if target and target["id"] != user["id"]:
            await conn.execute(
                "INSERT INTO follows (user_id, target_id, created_at) VALUES ($1, $2, $3) "
                "ON CONFLICT DO NOTHING",
                user["id"], target["id"], int(time.time()),
            )

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

    return RedirectResponse(f"/concert-tracker/u/{username}", status_code=302)


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


@router.get("/u/{username}", response_class=HTMLResponse)
async def friend_profile(
    username: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)
):
    async with pool.acquire() as conn:
        profile = await conn.fetchrow(
            "SELECT id, username, bio, avatar_url, created_at FROM users WHERE username = $1", username
        )
        if not profile:
            flash(request, "User not found", "error")
            return RedirectResponse("/concert-tracker/social", status_code=302)

        pid = profile["id"]
        uid = user["id"]

        shows = await conn.fetch(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM show_likes l WHERE l.show_id = s.id) AS like_count, "
            "(SELECT COUNT(*) FROM show_comments c WHERE c.show_id = s.id) AS comment_count "
            "FROM shows s WHERE s.user_id = $1 ORDER BY s.date DESC",
            pid,
        )
        is_following = await conn.fetchval(
            "SELECT 1 FROM follows WHERE user_id = $1 AND target_id = $2", uid, pid
        )
        is_follower = await conn.fetchval(
            "SELECT 1 FROM follows WHERE user_id = $1 AND target_id = $2", pid, uid
        )
        per_year = await conn.fetch(
            "SELECT EXTRACT(YEAR FROM date)::int AS year, COUNT(*)::int AS count "
            "FROM shows WHERE user_id=$1 GROUP BY year ORDER BY year", pid,
        )
        top_artists = await conn.fetch(
            "SELECT artist, COUNT(*)::int AS count FROM shows WHERE user_id=$1 "
            "GROUP BY artist ORDER BY count DESC LIMIT 5", pid,
        )
        top_venues = await conn.fetch(
            "SELECT venue, COUNT(*)::int AS count FROM shows WHERE user_id=$1 "
            "GROUP BY venue ORDER BY count DESC LIMIT 5", pid,
        )
        shared = await conn.fetch(
            "SELECT s.artist, s.date, s.venue, s.city, s.artist_thumb_url, s.is_festival, s.festival_name FROM shows s "
            "WHERE s.user_id = $1 AND EXISTS ("
            "  SELECT 1 FROM shows s2 WHERE s2.user_id = $2 "
            "  AND s2.artist = s.artist AND s2.date = s.date"
            ") ORDER BY s.date DESC",
            uid, pid,
        )
        show_count = await conn.fetchval(
            "SELECT (SELECT COUNT(*) FROM shows WHERE user_id=$1 AND (is_festival = FALSE OR festival_name IS NULL))"
            " + (SELECT COUNT(DISTINCT festival_name) FROM shows WHERE user_id=$1 AND is_festival = TRUE AND festival_name IS NOT NULL)",
            pid,
        )
        follower_count = await conn.fetchval("SELECT COUNT(*) FROM follows WHERE target_id=$1", pid)
        following_count = await conn.fetchval("SELECT COUNT(*) FROM follows WHERE user_id=$1", pid)

    seen_festivals: dict = {}
    items: list = []
    for row in shows:
        fid = row["festival_id"] if row["is_festival"] and row["festival_name"] else None
        if fid:
            key = str(fid)
            if key not in seen_festivals:
                entry: dict = {
                    "type": "festival",
                    "festival_id": key,
                    "festival_name": row["festival_name"],
                    "city": row["city"],
                    "date": row["date"],
                    "shows": [],
                }
                seen_festivals[key] = entry
                items.append(entry)
            seen_festivals[key]["shows"].append(row)
        else:
            items.append({"type": "show", "show": row})

    return templates.TemplateResponse(
        "profile.html",
        _ctx(
            request,
            user,
            profile=profile,
            items=items,
            show_count=show_count,
            follower_count=follower_count,
            following_count=following_count,
            is_following=bool(is_following),
            is_follower=bool(is_follower),
            is_mutual=bool(is_following and is_follower),
            is_own_profile=uid == pid,
            per_year=[dict(r) for r in per_year],
            top_artists=top_artists,
            top_venues=top_venues,
            shared=shared,
            csrf=get_csrf_token(request),
        ),
    )
