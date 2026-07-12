import os
import re
import time
import collections

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_pool
from app.auth import (
    flash, get_csrf_token, get_flashes, generate_invite_code,
    hash_password, require_user, verify_csrf, verify_password,
)
from app.jinja import templates

router = APIRouter()

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")

                                                              
_RATE_WINDOW = 300           
_RATE_LIMIT = 10
_rate_buckets: dict[str, collections.deque] = collections.defaultdict(collections.deque)

def _check_rate(ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.time()
    dq = _rate_buckets[ip]
    while dq and now - dq[0] > _RATE_WINDOW:
        dq.popleft()
    if len(dq) >= _RATE_LIMIT:
        return False
    dq.append(now)
    return True

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return forwarded.split(",")[0].strip() or request.client.host or "unknown"

def _ctx(request: Request, **kw) -> dict:
    return {"request": request, "flashes": get_flashes(request), "user": None, **kw}

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/concert-tracker/shows", status_code=302)
    return templates.TemplateResponse("login.html", _ctx(request, csrf=get_csrf_token(request)))

@router.post("/login")
async def login(request: Request, pool=Depends(get_pool)):
    if not _check_rate(_client_ip(request)):
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, csrf=get_csrf_token(request), error="Too many attempts - try again in a few minutes"),
            status_code=429,
        )
    await verify_csrf(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, password_hash, avatar_url, accent_color FROM users WHERE username = $1", username
        )

    if not row or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            "login.html",
            _ctx(request, csrf=get_csrf_token(request), error="Invalid username or password"),
            status_code=401,
        )

    request.session["user_id"] = row["id"]
    request.session["username"] = row["username"]
    request.session["avatar_url"] = row["avatar_url"]
    request.session["accent_color"] = row["accent_color"]
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/concert-tracker/login", status_code=302)

@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, code: str = ""):
    if request.session.get("user_id"):
        return RedirectResponse("/concert-tracker/shows", status_code=302)
    return templates.TemplateResponse(
        "register.html",
        _ctx(request, csrf=get_csrf_token(request), prefill_code=code),
    )

@router.post("/register")
async def register(request: Request, pool=Depends(get_pool)):
    if not _check_rate(_client_ip(request)):
        form = await request.form()
        invite_code = str(form.get("invite_code", "")).strip()
        return templates.TemplateResponse(
            "register.html",
            _ctx(request, csrf=get_csrf_token(request),
                 error="Too many attempts - try again in a few minutes", prefill_code=invite_code),
            status_code=429,
        )
    await verify_csrf(request)
    form = await request.form()
    username = str(form.get("username", "")).strip()
    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))
    invite_code = str(form.get("invite_code", "")).strip()

    def err(msg: str):
        return templates.TemplateResponse(
            "register.html",
            _ctx(request, csrf=get_csrf_token(request), error=msg, prefill_code=invite_code),
            status_code=400,
        )

    if not _USERNAME_RE.match(username):
        return err("Username must be 3–32 chars, letters/numbers/underscores only")
    if len(password) < 8:
        return err("Password must be at least 8 characters")
    if "@" not in email or len(email) > 254:
        return err("Enter a valid email address")
    if not invite_code:
        return err("An invite code is required")

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                code_row = await conn.fetchrow(
                    "SELECT created_by FROM invite_codes WHERE code = $1 AND used_by IS NULL",
                    invite_code,
                )
                if not code_row:
                    raise ValueError("invalid_invite")

                existing = await conn.fetchrow(
                    "SELECT username, email FROM users WHERE username = $1 OR email = $2", username, email
                )
                if existing:
                    if existing["username"] == username:
                        raise ValueError("username_taken")
                    raise ValueError("email_taken")

                now = int(time.time())
                user = await conn.fetchrow(
                    "INSERT INTO users (username, email, password_hash, created_at, invite_code_used) "
                    "VALUES ($1, $2, $3, $4, $5) RETURNING id, username",
                    username, email, hash_password(password), now, invite_code,
                )
                await conn.execute(
                    "UPDATE invite_codes SET used_by = $1, used_at = $2 WHERE code = $3",
                    user["id"], now, invite_code,
                )
    except ValueError as e:
        msg = {
            "invalid_invite": "Invite code is invalid or already used",
            "username_taken": "That username is taken - already have an account? Try logging in",
            "email_taken": "That email is already registered - already have an account? Try logging in",
        }.get(str(e), "Something went wrong, try again")
        return err(msg)

    request.session["user_id"] = user["id"]
    request.session["username"] = user["username"]
    request.session["avatar_url"] = None
    request.session["accent_color"] = None
    return RedirectResponse("/concert-tracker/shows", status_code=302)

@router.get("/invite", response_class=HTMLResponse)
async def invite_page(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    async with pool.acquire() as conn:
        codes = await conn.fetch(
            "SELECT code, created_at, used_by, used_at FROM invite_codes "
            "WHERE created_by = $1 ORDER BY created_at DESC LIMIT 50",
            user["id"],
        )
    base = str(request.base_url).rstrip("/")
    return templates.TemplateResponse(
        "invite.html",
        {**_ctx(request), "user": user, "codes": list(codes), "base_url": base,
         "flashes": get_flashes(request), "csrf": get_csrf_token(request)},
    )

@router.post("/invite")
async def create_invite(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    code = generate_invite_code()
    now = int(time.time())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO invite_codes (code, created_by, created_at) VALUES ($1, $2, $3)",
            code, user["id"], now,
        )
    flash(request, f"Invite code created: {code}", "success")
    return RedirectResponse("/concert-tracker/invite", status_code=302)

@router.post("/invite/{code}/revoke")
async def revoke_invite(code: str, request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM invite_codes WHERE code = $1 AND created_by = $2 AND used_by IS NULL",
            code, user["id"],
        )
    flash(request, "Invite revoked", "info")
    return RedirectResponse("/concert-tracker/invite", status_code=302)

@router.get("/profile/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, user=Depends(require_user)):
    return templates.TemplateResponse(
        "change_password.html",
        {"request": request, "user": user, "flashes": get_flashes(request), "csrf": get_csrf_token(request)},
    )

@router.post("/profile/change-password")
async def change_password(request: Request, pool=Depends(get_pool), user=Depends(require_user)):
    await verify_csrf(request)
    form = await request.form()
    current = str(form.get("current_password", ""))
    new_pw = str(form.get("new_password", ""))
    confirm = str(form.get("confirm_password", ""))

    def err(msg: str):
        return templates.TemplateResponse(
            "change_password.html",
            {"request": request, "user": user, "flashes": get_flashes(request),
             "csrf": get_csrf_token(request), "error": msg},
            status_code=400,
        )

    if len(new_pw) < 8:
        return err("New password must be at least 8 characters")
    if new_pw != confirm:
        return err("New passwords don't match")

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT password_hash FROM users WHERE id = $1", user["id"])
        if not row or not verify_password(current, row["password_hash"]):
            return err("Current password is incorrect")
        await conn.execute(
            "UPDATE users SET password_hash = $1 WHERE id = $2",
            hash_password(new_pw), user["id"],
        )

    flash(request, "Password updated", "success")
    return RedirectResponse(f"/concert-tracker/u/{user['username']}", status_code=302)

@router.post("/admin/invite")
async def admin_create_invite(request: Request, pool=Depends(get_pool)):
    token = request.headers.get("X-Admin-Token", "")
    expected = os.environ.get("ADMIN_SECRET", "")
    if not expected or not (token == expected):
        from fastapi import HTTPException
        raise HTTPException(403)
    code = generate_invite_code()
    now = int(time.time())
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO invite_codes (code, created_at) VALUES ($1, $2)", code, now
        )
    return {"code": code}
