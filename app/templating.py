"""Shared Jinja2 templates instance with custom filters.

All routes import `templates` from here so a filter registered once is
visible everywhere. Previously each route file created its own
`Jinja2Templates(...)` instance, so a filter would have to be re-registered
in four places.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_IST = timezone(timedelta(hours=5, minutes=30))


def ist(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a UTC datetime (aware or naive) in IST.

    All DB datetimes in this project are stored naive-UTC (datetime.utcnow).
    None returns "" so templates don't need an `if X else '—'` guard around
    every call — but existing guards keep working.
    """
    if value is None:
        return ""
    # Treat naive datetimes as UTC (which is what we write to the DB).
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_IST).strftime(fmt)


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["ist"] = ist
