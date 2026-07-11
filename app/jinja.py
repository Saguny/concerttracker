import datetime
from pathlib import Path
from urllib.parse import quote
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _timestamp_fmt(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%-d %b %Y")
    except Exception:
        return ""


templates.env.filters["timestamp_fmt"] = _timestamp_fmt
templates.env.filters["urlquote"] = lambda s: quote(str(s), safe="")
