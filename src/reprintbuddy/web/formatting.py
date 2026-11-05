"""Display formats. Times are stored as naive UTC and shown in Europe/Berlin."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from jinja2 import pass_context

LOCAL_ZONE = ZoneInfo("Europe/Berlin")

_FORMATS = {"de": "%d.%m.%Y %H:%M"}
_DEFAULT_FORMAT = "%Y-%m-%d %H:%M"


def to_local(dt: datetime) -> datetime:
    """A naive datetime means UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_ZONE)


def format_local(dt: datetime, lang: str) -> str:
    return to_local(dt).strftime(_FORMATS.get(lang, _DEFAULT_FORMAT))


@pass_context
def local_dt(context, dt: datetime | None) -> str:
    """Jinja filter `local_dt`, in the language of the request."""
    return format_local(dt, context.get("lang", "en")) if dt is not None else ""


def format_date(day: date, lang: str) -> str:
    return day.strftime("%d.%m.%Y" if lang == "de" else "%Y-%m-%d")


@pass_context
def local_date(context, day: date | None) -> str:
    """Jinja filter `local_date`: a calendar date (no time zone involved)."""
    return format_date(day, context.get("lang", "en")) if day is not None else ""
