import io
import os
import asyncio
import time
import boto3
from botocore.exceptions import ClientError

def _client():
    account_id = os.environ.get("R2_ACCOUNT_ID", "")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ.get("R2_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY", ""),
        region_name="auto",
    )

BUCKET = lambda: os.environ.get("R2_BUCKET", "social-credit-gacha")
PUBLIC_URL = lambda: os.environ.get("R2_PUBLIC_URL", "https://cdn.off-by-one.digital").rstrip("/")

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_BYTES = 15 * 1024 * 1024         

def _compress(data: bytes, content_type: str, max_w: int, max_h: int) -> tuple[bytes, str]:
    """Resize and re-encode to WebP. GIFs pass through unchanged."""
    if content_type == "image/gif":
        return data, content_type
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    img.thumbnail((max_w, max_h), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="WEBP", quality=82, method=4)
    return out.getvalue(), "image/webp"

async def upload_avatar(user_id: int, data: bytes, content_type: str) -> str:
    """Upload avatar bytes to R2, return the public URL. Raises ValueError on bad input."""
    if content_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type: {content_type}")
    if len(data) > MAX_BYTES:
        raise ValueError("File too large (max 15 MB)")

    key = f"avatars/{user_id}"

    def _run():
        compressed, ct = _compress(data, content_type, 512, 512)
        _client().put_object(
            Bucket=BUCKET(),
            Key=key,
            Body=compressed,
            ContentType=ct,
            CacheControl="public, max-age=31536000",
        )

    await asyncio.get_running_loop().run_in_executor(None, _run)
    return f"{PUBLIC_URL()}/{key}?v={int(time.time())}"

async def upload_show_photo(show_id: int, data: bytes, content_type: str) -> str:
    """Upload a show photo to R2, return the public URL. Raises ValueError on bad input."""
    if content_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type: {content_type}")
    if len(data) > MAX_BYTES:
        raise ValueError("File too large (max 15 MB)")

    key = f"show-photos/{show_id}"

    def _run():
        compressed, ct = _compress(data, content_type, 1920, 1920)
        _client().put_object(
            Bucket=BUCKET(),
            Key=key,
            Body=compressed,
            ContentType=ct,
            CacheControl="public, max-age=31536000",
        )

    await asyncio.get_running_loop().run_in_executor(None, _run)
    return f"{PUBLIC_URL()}/{key}?v={int(time.time())}"

async def upload_banner(user_id: int, data: bytes, content_type: str) -> str:
    """Upload banner bytes to R2, return the public URL. Raises ValueError on bad input."""
    if content_type not in ALLOWED_TYPES:
        raise ValueError(f"Unsupported file type: {content_type}")
    if len(data) > MAX_BYTES:
        raise ValueError("File too large (max 15 MB)")

    key = f"banners/{user_id}"

    def _run():
        compressed, ct = _compress(data, content_type, 1920, 2400)
        _client().put_object(
            Bucket=BUCKET(),
            Key=key,
            Body=compressed,
            ContentType=ct,
            CacheControl="public, max-age=31536000",
        )

    await asyncio.get_running_loop().run_in_executor(None, _run)
    return f"{PUBLIC_URL()}/{key}?v={int(time.time())}"
