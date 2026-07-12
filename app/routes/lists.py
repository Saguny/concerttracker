import asyncio
import json
import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.db import get_pool
from app.auth import flash, get_csrf_token, get_flashes, require_user, optional_user, verify_csrf
from app.jinja import templates

router = APIRouter()


async def _smart_items(conn, user_id: int, smart_filter: dict) -> list:
    ftype = smart_filter.get("type", "")
    clauses = ["user_id = $1"]
    params: list = [user_id]

    if ftype == "year":
        params.append(int(smart_filter["value"]))
        clauses.append(f"EXTRACT(YEAR FROM date) = ${len(params)}")
    elif ftype == "rating_min":
        params.append(float(smart_filter["value"]))
        clauses.append(f"rating >= ${len(params)}")
    elif ftype == "show_type":
        if smart_filter["value"] == "festival":
            clauses.append("is_festival = TRUE")
        else:
            clauses.append("(is_festival = FALSE OR is_festival IS NULL)")

    where = " AND ".join(clauses)
    rows = await conn.fetch(
        f"SELECT id AS show_id, artist, venue, city, date, artist_thumb_url, rating, 0 AS position "
        f"FROM shows WHERE {where} ORDER BY date DESC",
        *params,
    )
    return [dict(r) for r in rows]


def _ctx(request: Request, user, **kw) -> dict:
    return {"request": request, "user": user, "flashes": get_flashes(request), **kw}


def _is_ajax(request: Request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


@router.get("/lists", response_class=HTMLResponse)
async def lists_index(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT l.*, COUNT(li.id)::int AS item_count "
            "FROM lists l LEFT JOIN list_items li ON li.list_id = l.id "
            "WHERE l.user_id = $1 GROUP BY l.id ORDER BY l.updated_at DESC",
            user["id"],
        )
        year_rows = await conn.fetch(
            "SELECT DISTINCT EXTRACT(YEAR FROM date)::int AS y FROM shows WHERE user_id=$1 ORDER BY y DESC",
            user["id"],
        )
    return templates.TemplateResponse(
        "lists.html",
        _ctx(request, user, lists=list(rows), years=[r["y"] for r in year_rows], csrf=get_csrf_token(request)),
    )


@router.post("/lists/create")
async def create_list(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    title = str(form.get("title", "")).strip()[:200]
    is_ranked = form.get("is_ranked") == "1"
    list_type = form.get("list_type", "curated")
    if list_type not in ("curated", "smart"):
        list_type = "curated"
    if not title:
        flash(request, "Title is required", "error")
        return RedirectResponse("/concert-tracker/lists", status_code=302)

    smart_filter = None
    if list_type == "smart":
        ftype = str(form.get("smart_type", "")).strip()
        fval = str(form.get("smart_value", "")).strip()
        if ftype and fval:
            smart_filter = json.dumps({"type": ftype, "value": fval})
        else:
            flash(request, "Smart list requires a filter type and value", "error")
            return RedirectResponse("/concert-tracker/lists", status_code=302)

    now = int(time.time())
    async with pool.acquire() as conn:
        lst_id = await conn.fetchval(
            "INSERT INTO lists (user_id, title, is_ranked, list_type, smart_filter, created_at, updated_at) "
            "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $6) RETURNING id",
            user["id"], title, is_ranked, list_type, smart_filter, now,
        )
    return RedirectResponse(f"/concert-tracker/lists/{lst_id}", status_code=302)


@router.get("/lists/{list_id}", response_class=HTMLResponse)
async def list_detail(list_id: int, request: Request, pool=Depends(get_pool), user=Depends(optional_user)):
    async with pool.acquire() as conn:
        lst = await conn.fetchrow(
            "SELECT l.*, u.username AS owner_username FROM lists l "
            "JOIN users u ON u.id = l.user_id WHERE l.id = $1",
            list_id,
        )
        if not lst:
            return RedirectResponse("/concert-tracker/lists" if user else "/concert-tracker/login", status_code=302)
        lst = dict(lst)
        is_owner = user is not None and user["id"] == lst["user_id"]
        if lst.get("list_type") == "smart" and lst.get("smart_filter"):
            items = await _smart_items(conn, lst["user_id"], lst["smart_filter"])
        else:
            rows = await conn.fetch(
                "SELECT li.id, li.show_id, li.position, "
                "s.artist, s.venue, s.city, s.date, s.artist_thumb_url, s.rating "
                "FROM list_items li JOIN shows s ON s.id = li.show_id "
                "WHERE li.list_id = $1 ORDER BY li.position, li.added_at",
                list_id,
            )
            items = [dict(r) for r in rows]
    return templates.TemplateResponse(
        "list_detail.html",
        _ctx(
            request, user,
            lst=lst,
            items=items,
            is_owner=is_owner,
            csrf=get_csrf_token(request) if user else "",
        ),
    )


@router.get("/lists/{list_id}/edit", response_class=HTMLResponse)
async def edit_list_page(list_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        lst = await conn.fetchrow("SELECT * FROM lists WHERE id=$1 AND user_id=$2", list_id, user["id"])
        if not lst:
            return RedirectResponse("/concert-tracker/lists", status_code=302)
        year_rows = await conn.fetch(
            "SELECT DISTINCT EXTRACT(YEAR FROM date)::int AS y FROM shows WHERE user_id=$1 ORDER BY y DESC",
            user["id"],
        )
    return templates.TemplateResponse(
        "list_edit.html",
        _ctx(request, user, lst=dict(lst), years=[r["y"] for r in year_rows], csrf=get_csrf_token(request)),
    )


@router.post("/lists/{list_id}/edit")
async def edit_list(list_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    title = str(form.get("title", "")).strip()[:200]
    description = str(form.get("description", "")).strip()[:1000] or None
    is_ranked = form.get("is_ranked") == "1"
    if not title:
        flash(request, "Title is required", "error")
        return RedirectResponse(f"/concert-tracker/lists/{list_id}/edit", status_code=302)
    now = int(time.time())
    async with pool.acquire() as conn:
        lst = await conn.fetchrow("SELECT list_type FROM lists WHERE id=$1 AND user_id=$2", list_id, user["id"])
        if not lst:
            return RedirectResponse("/concert-tracker/lists", status_code=302)
        smart_filter = None
        if lst["list_type"] == "smart":
            ftype = str(form.get("smart_type", "")).strip()
            fval = str(form.get("smart_value", "")).strip()
            if ftype and fval:
                smart_filter = json.dumps({"type": ftype, "value": fval})
        await conn.execute(
            "UPDATE lists SET title=$1, description=$2, is_ranked=$3, smart_filter=COALESCE($4::jsonb, smart_filter), updated_at=$5 "
            "WHERE id=$6 AND user_id=$7",
            title, description, is_ranked, smart_filter, now, list_id, user["id"],
        )
    return RedirectResponse(f"/concert-tracker/lists/{list_id}", status_code=302)


@router.post("/lists/{list_id}/delete")
async def delete_list(list_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM lists WHERE id=$1 AND user_id=$2", list_id, user["id"])
    return RedirectResponse("/concert-tracker/lists", status_code=302)


@router.get("/lists/{list_id}/add", response_class=HTMLResponse)
async def add_shows_page(
    list_id: int,
    request: Request,
    pool=Depends(get_pool),
    user=Depends(require_user),
    year: str = "",
    artist_filter: str = "",
    sort: str = "date_desc",
):
    order = {
        "date_desc": "s.date DESC",
        "date_asc": "s.date ASC",
        "artist": "s.artist ASC",
        "rating_desc": "s.rating DESC NULLS LAST",
    }.get(sort, "s.date DESC")
    async with pool.acquire() as conn:
        lst = await conn.fetchrow("SELECT * FROM lists WHERE id=$1 AND user_id=$2", list_id, user["id"])
        if not lst:
            return RedirectResponse("/concert-tracker/lists", status_code=302)
        clauses = ["s.user_id = $1"]
        params: list = [user["id"]]
        if year:
            params.append(int(year))
            clauses.append(f"EXTRACT(YEAR FROM s.date) = ${len(params)}")
        if artist_filter:
            params.append(f"%{artist_filter.lower()}%")
            clauses.append(f"LOWER(s.artist) LIKE ${len(params)}")
        where = " AND ".join(clauses)
        shows = await conn.fetch(
            f"SELECT s.id, s.artist, s.venue, s.city, s.date, s.artist_thumb_url, s.rating "
            f"FROM shows s WHERE {where} ORDER BY {order}",
            *params,
        )
        in_list_rows = await conn.fetch(
            "SELECT li.id AS item_id, li.show_id FROM list_items li WHERE li.list_id=$1",
            list_id,
        )
        year_rows = await conn.fetch(
            "SELECT DISTINCT EXTRACT(YEAR FROM date)::int AS y FROM shows WHERE user_id=$1 ORDER BY y DESC",
            user["id"],
        )
    in_list = {r["show_id"]: r["item_id"] for r in in_list_rows}
    return templates.TemplateResponse(
        "list_add.html",
        _ctx(
            request, user,
            lst=lst,
            shows=list(shows),
            in_list=in_list,
            years=[r["y"] for r in year_rows],
            filters={"year": year, "artist": artist_filter, "sort": sort},
            csrf=get_csrf_token(request),
        ),
    )


@router.post("/lists/{list_id}/add")
async def add_show(list_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    show_id_raw = str(form.get("show_id", ""))
    if not show_id_raw.isdigit():
        if _is_ajax(request):
            return JSONResponse({"error": "invalid"}, status_code=400)
        return RedirectResponse(f"/concert-tracker/lists/{list_id}/add", status_code=302)
    show_id = int(show_id_raw)
    now = int(time.time())
    async with pool.acquire() as conn:
        lst = await conn.fetchrow("SELECT id FROM lists WHERE id=$1 AND user_id=$2", list_id, user["id"])
        if not lst:
            if _is_ajax(request):
                return JSONResponse({"error": "not found"}, status_code=404)
            return RedirectResponse("/concert-tracker/lists", status_code=302)
        show = await conn.fetchrow("SELECT id FROM shows WHERE id=$1 AND user_id=$2", show_id, user["id"])
        if not show:
            if _is_ajax(request):
                return JSONResponse({"error": "not found"}, status_code=404)
            return RedirectResponse(f"/concert-tracker/lists/{list_id}/add", status_code=302)
        max_pos = await conn.fetchval(
            "SELECT COALESCE(MAX(position), 0) FROM list_items WHERE list_id=$1", list_id
        )
        try:
            item_id = await conn.fetchval(
                "INSERT INTO list_items (list_id, show_id, position, added_at) "
                "VALUES ($1, $2, $3, $4) RETURNING id",
                list_id, show_id, max_pos + 1, now,
            )
        except Exception:
            item_id = await conn.fetchval(
                "SELECT id FROM list_items WHERE list_id=$1 AND show_id=$2", list_id, show_id
            )
        await conn.execute("UPDATE lists SET updated_at=$1 WHERE id=$2", now, list_id)
    if _is_ajax(request):
        return JSONResponse({"ok": True, "item_id": item_id})
    return RedirectResponse(f"/concert-tracker/lists/{list_id}/add", status_code=302)


@router.post("/lists/{list_id}/items/{item_id}/remove")
async def remove_item(
    list_id: int,
    item_id: int,
    request: Request,
    pool=Depends(get_pool),
    user=Depends(require_user),
):
    await verify_csrf(request)
    now = int(time.time())
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM list_items li USING lists l "
            "WHERE li.id=$1 AND li.list_id=$2 AND l.id=$2 AND l.user_id=$3",
            item_id, list_id, user["id"],
        )
        await conn.execute(
            "UPDATE lists SET updated_at=$1 WHERE id=$2 AND user_id=$3", now, list_id, user["id"]
        )
    if _is_ajax(request):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"/concert-tracker/lists/{list_id}", status_code=302)


@router.post("/lists/{list_id}/reorder")
async def reorder_items(list_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    raw = str(form.get("item_ids", ""))
    ids = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    if not ids:
        return JSONResponse({"ok": True})
    now = int(time.time())
    async with pool.acquire() as conn:
        lst = await conn.fetchrow("SELECT id FROM lists WHERE id=$1 AND user_id=$2", list_id, user["id"])
        if not lst:
            return JSONResponse({"error": "not found"}, status_code=404)
        await conn.executemany(
            "UPDATE list_items SET position=$1 WHERE id=$2 AND list_id=$3",
            [(pos + 1, iid, list_id) for pos, iid in enumerate(ids)],
        )
        await conn.execute("UPDATE lists SET updated_at=$1 WHERE id=$2", now, list_id)
    return JSONResponse({"ok": True})
