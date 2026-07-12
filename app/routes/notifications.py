import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.db import get_pool
from app.auth import require_user

router = APIRouter()

_LIMIT = 30

async def create_notification(pool, *, user_id: int, actor_id: int, type: str,
                               show_id: int | None = None, festival_id: int | None = None,
                               comment_id: int | None = None):
    if user_id == actor_id:
        return
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO notifications (user_id, actor_id, type, show_id, festival_id, comment_id, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT DO NOTHING",
            user_id, actor_id, type, show_id, festival_id, comment_id, int(time.time()),
        )

@router.get("/api/notifications")
async def get_notifications(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT n.id, n.type, n.show_id, n.festival_id, n.comment_id, n.is_read, n.created_at, "
            "u.username AS actor_username, u.avatar_url AS actor_avatar, "
            "s.artist AS show_artist "
            "FROM notifications n JOIN users u ON u.id = n.actor_id "
            "LEFT JOIN shows s ON s.id = n.show_id "
            "WHERE n.user_id = $1 ORDER BY n.created_at DESC LIMIT $2",
            user["id"], _LIMIT,
        )
    return [dict(r) for r in rows]

@router.get("/api/notifications/unread-count")
async def unread_count(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM notifications WHERE user_id = $1 AND is_read = FALSE",
            user["id"],
        )
    return {"count": int(count)}

@router.post("/api/notifications/read")
async def mark_all_read(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notifications SET is_read = TRUE WHERE user_id = $1 AND is_read = FALSE",
            user["id"],
        )
    return JSONResponse({"ok": True})

@router.post("/api/notifications/{notif_id}/read")
async def mark_one_read(notif_id: int, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE notifications SET is_read = TRUE WHERE id = $1 AND user_id = $2",
            notif_id, user["id"],
        )
    return JSONResponse({"ok": True})
