import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.db import init_pool, close_pool
from app.redis_client import close_redis
from app.auth import NotAuthenticatedException
import app.setlistfm as setlistfm
import app.spotify as spotify
import app.musicbrainz as musicbrainz
import app.lastfm as lastfm

from app.routes import auth, shows, stats, social, artists
from app.routes import calendar as cal


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_pool()
    yield
    await close_pool()
    await close_redis()
    await setlistfm.close()
    await spotify.close()
    await musicbrainz.close()
    await lastfm.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

_secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")
if _secret_key == "dev-secret-change-in-prod":
    print("WARNING: SECRET_KEY not set - using insecure default. Set SECRET_KEY in production.", flush=True)

app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key,
    max_age=30 * 24 * 3600,
    https_only=os.environ.get("ENV") == "production",
    same_site="strict",
)

_base = "/concert-tracker"

app.mount(f"{_base}/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.include_router(auth.router, prefix=_base)
app.include_router(shows.router, prefix=_base)
app.include_router(artists.router, prefix=_base)
app.include_router(stats.router, prefix=_base)
app.include_router(social.router, prefix=_base)
app.include_router(cal.router, prefix=_base)


class _CSPMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://maps.googleapis.com https://maps.gstatic.com; "
            "connect-src 'self' https://*.googleapis.com https://*.gstatic.com; "
            "img-src 'self' data: https://cdn.off-by-one.digital https://*.googleapis.com https://*.gstatic.com https://*.ggpht.com https://i.scdn.co https://lastfm.freetls.fastly.net; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "frame-src 'none';"
        )
        return response

app.add_middleware(_CSPMiddleware)


@app.exception_handler(NotAuthenticatedException)
async def _not_auth(request: Request, exc: NotAuthenticatedException):
    return RedirectResponse(f"{_base}/login", status_code=302)


@app.get(f"{_base}")
@app.get(f"{_base}/")
async def index():
    return RedirectResponse(f"{_base}/social", status_code=302)
