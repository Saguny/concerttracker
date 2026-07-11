import calendar
import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.db import get_pool
from app.auth import get_flashes, require_user
from app.jinja import templates

router = APIRouter()


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(
    request: Request,
    pool=Depends(get_pool),
    user=Depends(require_user),
    year: int = 0,
    month: int = 0,
):
    today = datetime.date.today()
    if not year:
        year = today.year
    if not month:
        month = today.month

    month = max(1, min(12, month))
    year = max(2000, min(2100, year))

    first_day = datetime.date(year, month, 1)
    last_day = datetime.date(year, month, calendar.monthrange(year, month)[1])

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, artist, venue, city, date, is_festival, festival_name, artist_thumb_url "
            "FROM shows WHERE user_id = $1 AND date >= $2 AND date <= $3 ORDER BY date",
            user["id"], first_day, last_day,
        )

    shows_by_day: dict[int, list] = {}
    for row in rows:
        d = row["date"].day
        shows_by_day.setdefault(d, []).append(dict(row))

    # Build 6-row calendar grid (Mon–Sun)
    cal_grid = calendar.monthcalendar(year, month)

    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    month_name = first_day.strftime("%B %Y")

    return templates.TemplateResponse(
        "calendar.html",
        {
            "request": request,
            "user": user,
            "flashes": get_flashes(request),
            "cal_grid": cal_grid,
            "shows_by_day": shows_by_day,
            "year": year,
            "month": month,
            "month_name": month_name,
            "today": today,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
        },
    )
