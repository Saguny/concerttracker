import datetime
import html as _html
import re as _re
from pathlib import Path
from urllib.parse import quote
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_MENTION_RE = _re.compile(r'@([A-Za-z0-9_]{2,30})')

def render_mentions(text: str) -> Markup:
    escaped = _html.escape(str(text or ''))
    rendered = _MENTION_RE.sub(
        lambda m: f'<a class="mention" href="/concert-tracker/u/{m.group(1)}">@{m.group(1)}</a>',
        escaped,
    )
    return Markup(rendered)

def _timestamp_fmt(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%-d %b %Y")
    except Exception:
        return ""

templates.env.filters["timestamp_fmt"] = _timestamp_fmt
templates.env.filters["datetimeformat"] = _timestamp_fmt
templates.env.filters["urlquote"] = lambda s: quote(str(s), safe="")
templates.env.filters["render_mentions"] = render_mentions
