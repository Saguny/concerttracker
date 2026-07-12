import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db import get_pool
from app.auth import get_csrf_token, get_flashes, require_user
from app.jinja import templates, render_mentions as _render_mentions
import app.lastfm as lastfm
import app.spotify as spotify

router = APIRouter()


def _ctx(request: Request, user: dict, **kw) -> dict:
    return {"request": request, "user": user, "flashes": get_flashes(request), **kw}


@router.get("/artists/{name}", response_class=HTMLResponse)
async def artist_page(name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        artist_row = await conn.fetchrow("SELECT * FROM artists WHERE LOWER(name) = LOWER($1)", name)
        rows = await conn.fetch(
            "SELECT * FROM shows WHERE LOWER(artist) = LOWER($1) AND user_id = $2 ORDER BY date DESC",
            name, user["id"],
        )
        comments = await conn.fetch(
            "SELECT ac.id, ac.body, ac.created_at, u.username, u.avatar_url "
            "FROM artist_comments ac JOIN users u ON u.id = ac.user_id "
            "WHERE LOWER(ac.artist_name) = LOWER($1) ORDER BY ac.created_at ASC",
            name,
        )

    artist_name = artist_row["name"] if artist_row else (rows[0]["artist"] if rows else name)
    lfm = await lastfm.get_artist_info(artist_name)

    if not rows and not lfm and not artist_row:
        return RedirectResponse("/concert-tracker/shows", status_code=302)

    db_image = (artist_row["image_url"] if artist_row else None) or (rows[0]["artist_image_url"] if rows else None)
    db_spotify_id = (artist_row["spotify_id"] if artist_row else None) or (rows[0]["artist_spotify_id"] if rows else None)
    db_genres = ((artist_row["genres"] or []) if artist_row else None) or (rows[0]["artist_genres"] or [] if rows else [])

    sp = None
    if not db_image or not db_spotify_id:
        sp = await spotify.search_artist(artist_name)
        if sp:
            now = int(time.time())
            async with pool.acquire() as conn:
                await conn.execute(
                    """INSERT INTO artists (name, spotify_id, image_url, thumb_url, genres, created_at, updated_at)
                       VALUES ($1, $2, $3, $4, $5, $6, $6)
                       ON CONFLICT (name) DO UPDATE SET
                           spotify_id = COALESCE(EXCLUDED.spotify_id, artists.spotify_id),
                           image_url  = COALESCE(EXCLUDED.image_url,  artists.image_url),
                           thumb_url  = COALESCE(EXCLUDED.thumb_url,  artists.thumb_url),
                           genres     = COALESCE(EXCLUDED.genres,     artists.genres),
                           updated_at = EXCLUDED.updated_at""",
                    artist_name, sp["id"], sp["image_url"], sp["thumb_url"], sp.get("genres"), now,
                )

    image_url = db_image or (sp["image_url"] if sp else None)
    spotify_id = db_spotify_id or (sp["id"] if sp else None)
    genres = db_genres or (sp.get("genres") or [] if sp else [])
    spotify_url = f"https://open.spotify.com/artist/{spotify_id}" if spotify_id else None

    return templates.TemplateResponse(
        "artist.html",
        _ctx(
            request, user,
            artist_name=artist_name,
            image_url=image_url,
            genres=genres,
            spotify_url=spotify_url,
            lfm=lfm,
            shows=rows,
            comments=list(comments),
            csrf=get_csrf_token(request),
        ),
    )


def _is_ajax(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


@router.post("/artists/{name}/comments")
async def add_artist_comment(name: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    from app.auth import verify_csrf
    await verify_csrf(request)
    form = await request.form()
    body = str(form.get("body", "")).strip()[:500]
    comment_row = None
    if body:
        async with pool.acquire() as conn:
            now = int(time.time())
            comment_id = await conn.fetchval(
                "INSERT INTO artist_comments (artist_name, user_id, body, created_at) VALUES ($1, $2, $3, $4) RETURNING id",
                name, user["id"], body, now,
            )
            comment_row = {"id": comment_id, "body": str(_render_mentions(body)), "created_at": now,
                           "username": user["username"], "avatar_url": user.get("avatar_url")}
    if _is_ajax(request):
        return JSONResponse(comment_row or {"error": "empty"})
    return RedirectResponse(f"/concert-tracker/artists/{name}", status_code=302)


@router.post("/artists/{name}/comments/{comment_id}/delete")
async def delete_artist_comment(name: str, comment_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    from app.auth import verify_csrf
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM artist_comments WHERE id = $1 AND user_id = $2", comment_id, user["id"])
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/concert-tracker/artists/{name}", status_code=302)
