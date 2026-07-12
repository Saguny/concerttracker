import secrets
from passlib.context import CryptContext
from fastapi import Request, HTTPException

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")


class NotAuthenticatedException(Exception):
    pass


def hash_password(password: str) -> str:
    return pwd_ctx.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_ctx.verify(plain, hashed)


def get_current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return {
        "id": int(user_id),
        "username": request.session.get("username"),
        "avatar_url": request.session.get("avatar_url"),
        "accent_color": request.session.get("accent_color"),
    }


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise NotAuthenticatedException()
    return user


def get_csrf_token(request: Request) -> str:
    if "_csrf" not in request.session:
        request.session["_csrf"] = secrets.token_hex(32)
    return request.session["_csrf"]


async def verify_csrf(request: Request) -> None:
    form = await request.form()
    token = form.get("_csrf", "")
    expected = request.session.get("_csrf", "")
    if not secrets.compare_digest(str(token), str(expected)):
        raise HTTPException(403, "Invalid CSRF token")


def flash(request: Request, message: str, category: str = "info") -> None:
    msgs = request.session.get("_flashes", [])
    msgs.append({"message": message, "category": category})
    request.session["_flashes"] = msgs


def get_flashes(request: Request) -> list:
    msgs = request.session.get("_flashes", [])
    request.session["_flashes"] = []
    return msgs


def generate_invite_code() -> str:
    return secrets.token_urlsafe(16)
