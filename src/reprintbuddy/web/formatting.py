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


def format_decimal(value: float, digits: int, lang: str) -> str:
    """A number for display with `digits` decimals: a comma in German, a point otherwise (p58).

    This is the one place that decides the separator. Display values are never written with
    `f"{x:.1f}"` (always a point); `tests/web/test_decimal_display.py` keeps the routes honest.
    """
    text = f"{value:.{digits}f}"
    return text.replace(".", ",") if lang == "de" else text


@pass_context
def decimal(context, value: float | None, digits: int = 1) -> str:
    """Jinja filter `num`: `{{ grams|num }}` or `{{ x|num(2) }}`, in the language of the request."""
    return format_decimal(value, digits, context.get("lang", "en")) if value is not None else ""


def fill_percent(available: int, target: int) -> int | None:
    """How full a stock is against its target, 0 to 100; `None` without a target."""
    if target <= 0:
        return None
    return min(100, max(0, available) * 100 // target)
