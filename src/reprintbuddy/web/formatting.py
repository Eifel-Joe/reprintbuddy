"""Display formats. Times are stored as naive UTC and shown in the time zone of the server.

p183 (v0.3.14): they were shown fixed in Europe/Berlin, wherever Reprintbuddy ran. Now the zone the machine
is set to (`timedatectl set-timezone`, or `TZ` for the service) decides, with its own summer time.
Rejected for now: a setting under Settings - the zone of the server is what the machine already knows.
Tests: tests/web/test_server_time_zone.py; the other tests pin a zone (`conftest.fixed_display_zone`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, tzinfo

from jinja2 import pass_context

SERVER_ZONE: tzinfo | None = None  # `astimezone(None)`: the local zone of the machine, at that moment
LOCAL_ZONE: tzinfo | None = SERVER_ZONE  # the zone times are shown in; tests pin one

_FORMATS = {"de": "%d.%m.%Y %H:%M"}
_DEFAULT_FORMAT = "%Y-%m-%d %H:%M"


def to_local(dt: datetime) -> datetime:
    """A naive datetime means UTC; the result is in the zone of the server (`LOCAL_ZONE`)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(LOCAL_ZONE)


def local_day_end(day: date) -> datetime:
    """The last moment of `day` in the zone of the display, as naive UTC like every stored time.

    p260: an inventory dated back holds for the end of that day (Spec 6.2), not for the clock time it
    is typed in at; a sale in the evening of that day lies before the count. The zone is the one
    that decides "today" for the date field. Taken through the zone itself (not today's offset), so
    a day with a change of summer time ends at its own 23:59:59. Tests: tests/web/test_bookings_pages.py.
    """
    end = datetime.combine(day, time.max)
    local = end.astimezone() if LOCAL_ZONE is None else end.replace(tzinfo=LOCAL_ZONE)
    return local.astimezone(UTC).replace(tzinfo=None)


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
def local_day(context, dt: datetime | None) -> str:
    """Jinja filter `local_day`: the local calendar day of a moment (a naive one means UTC)."""
    return format_date(to_local(dt).date(), context.get("lang", "en")) if dt is not None else ""


@pass_context
def decimal(context, value: float | None, digits: int = 1) -> str:
    """Jinja filter `num`: `{{ grams|num }}` or `{{ x|num(2) }}`, in the language of the request."""
    return format_decimal(value, digits, context.get("lang", "en")) if value is not None else ""


# The default unit of a part is stored as "pcs" (no migration) and shown in the language of the interface.
DEFAULT_UNIT = "pcs"
_DEFAULT_UNIT_SHOWN = {"de": "Stk."}


def unit_label(value: str, lang: str) -> str:
    """A unit for display: the default "pcs" reads "Stk." in German; any other unit is shown as typed."""
    if value == DEFAULT_UNIT:
        return _DEFAULT_UNIT_SHOWN.get(lang, DEFAULT_UNIT)
    return value


def canonical_unit(typed: str) -> str:
    """A unit from a form: what the form showed for the default ("Stk." or "pcs", any case) is stored as "pcs"."""
    text = typed.strip()
    return DEFAULT_UNIT if text.lower() in {DEFAULT_UNIT, "stk."} else text


@pass_context
def unit(context, value: str | None) -> str:
    """Jinja filter `unit`: `{{ part.unit|unit }}`, in the language of the request."""
    return unit_label(value, context.get("lang", "en")) if value else ""


def fill_percent(available: int, target: int) -> int | None:
    """How full a stock is against its target, 0 to 100; `None` without a target."""
    if target <= 0:
        return None
    return min(100, max(0, available) * 100 // target)
