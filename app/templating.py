"""Shared Jinja2 templates instance with custom filters.

All routes import `templates` from here so a filter registered once is
visible everywhere. Previously each route file created its own
`Jinja2Templates(...)` instance, so a filter would have to be re-registered
in four places.
"""
from datetime import UTC, datetime, timedelta, timezone
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
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_IST).strftime(fmt)


def humanize_duration(seconds: float | int | None) -> str:
    """Compact human duration: '4s', '37s', '2m 15s', '1h 4m', '—' for None."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def job_eta_seconds(processed: int, total: int, started_at: datetime | None) -> float | None:
    """Estimate remaining seconds for a running job.

    Returns None when an estimate isn't meaningful yet (no progress, or no
    start time). Caller can pipe through `humanize_duration` for display.
    """
    if not started_at or total <= 0 or processed <= 0 or processed >= total:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    if elapsed <= 0:
        return None
    rate = processed / elapsed  # emails/sec
    remaining_emails = total - processed
    return remaining_emails / rate


templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.env.filters["ist"] = ist
templates.env.filters["duration"] = humanize_duration
