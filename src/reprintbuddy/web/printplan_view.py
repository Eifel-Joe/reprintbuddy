"""The print plan of the overview laid out for the page (Spec 10.2, 0.4.2): time axis, bars, texts.

The assignment itself is `domain.printplan.build_plan`; this module feeds it from the printers' state in
memory (`PrinterBoard`, Spec 7.6) and Bambuddy's queue as the last query read it (`EngineStatus.bambuddy_queue`),
and turns its times into positions on the axis and local clock times. Nothing here asks Bambuddy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.types import QueueItem
from reprintbuddy.domain import dispatch, workqueue
from reprintbuddy.domain.printerboard import PrinterBoard
from reprintbuddy.domain.printplan import Bar, PlanItem, PlanPrinter, build_plan
from reprintbuddy.localtime import to_local
from reprintbuddy.web.i18n import translate

MIN_SPAN = timedelta(hours=2)  # the axis reaches from now to "alles fertig", at least 2 h (Spec 10.2)
MAX_SPAN = timedelta(hours=24)  # and at most 24 h; a bar beyond ends at the edge with "→ bis …"
STALE_AFTER = timedelta(minutes=5)  # "Stand HH:MM" when the printers' state is older (Spec 10.2)

_KIND_KEYS = {
    "running": "overview.plan.kind_running",
    "fixed": "overview.plan.kind_fixed",
    "planned": "overview.plan.kind_planned",
}
_CONDITION_KEYS = {
    "offline": "overview.plan.condition_offline",
    "maintenance": "overview.plan.condition_maintenance",
    "error": "overview.plan.condition_error",
    "plate": "overview.plan.condition_plate",
}


@dataclass(frozen=True)
class Axis:
    start: datetime
    end: datetime
    ticks: tuple[tuple[float, str], ...]  # (percent from the left, local clock time)


@dataclass(frozen=True)
class BarView:
    kind: str
    kind_text: str  # the kind in words (for screen readers and the phone list)
    label: str
    own: bool
    left: float  # percent of the axis
    width: float
    start_text: str
    end_text: str
    detail: str  # "63 % · 1 h 10 left", "paused", "Duration unknown"
    cut: str  # "→ until Fri 16:00" when the bar goes beyond the axis
    progress: float | None
    beyond: bool = False  # starts after the end of the axis: no place on it (only the phone list shows it)


@dataclass(frozen=True)
class LaneView:
    name: str
    condition: str
    condition_text: str
    bars: tuple[BarView, ...]
    waiting: tuple[str, ...]


@dataclass(frozen=True)
class PlanView:
    known: bool
    lanes: tuple[LaneView, ...] = ()
    ticks: tuple[tuple[float, str], ...] = ()
    finish: str = ""  # local clock time of the end of the last bar, "" without bars
    minimum: bool = False  # a duration was unknown: "mindestens ca."
    without: tuple[str, ...] = ()
    unplaced: int = 0
    manual: int = 0
    waiting_here: int = 0  # prints in Reprintbuddy's own queue (held ones too)
    stale: str = ""  # local time of the printers' state when older than 5 minutes


# ---- times --------------------------------------------------------------------------------------


def _clock(moment: datetime) -> str:
    return to_local(moment).strftime("%H:%M")


def _when(moment: datetime, now: datetime, lang: str) -> str:
    """Clock time; on another day than today with the short weekday ("Fri 16:00")."""
    local = to_local(moment)
    if local.date() == to_local(now).date():
        return local.strftime("%H:%M")
    weekday = translate(lang, "overview.plan.weekdays").split(",")[local.weekday()]
    return f"{weekday} {local.strftime('%H:%M')}"


def _span(delta: timedelta) -> str:
    minutes = max(0, round(delta.total_seconds() / 60))
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest:02d}" if hours else f"{rest} min"


def axis(now: datetime, finish: datetime | None) -> Axis:
    """From now to the end of the last print, at least 2 and at most 24 hours. Ticks on full local hours
    after now: every hour up to 8 hours, every 2 up to 16, else every 4 (Spec 10.2). Walked in quarter
    hours of UTC, so zones with half hours and the change of summer time are right."""
    end = now + MIN_SPAN if finish is None else min(max(finish, now + MIN_SPAN), now + MAX_SPAN)
    span = end - now
    step = 1 if span <= timedelta(hours=8) else 2 if span <= timedelta(hours=16) else 4
    ticks: list[tuple[float, str]] = []
    moment = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    while moment <= end:
        local = to_local(moment)
        if moment > now and local.minute == 0 and local.hour % step == 0:
            ticks.append((100 * (moment - now) / span, local.strftime("%H:%M")))
        moment += timedelta(minutes=15)
    return Axis(now, end, tuple(ticks))


# ---- input of the plan --------------------------------------------------------------------------


def _file_label(item: QueueItem) -> str:
    name = item.file_name or ""
    for suffix in (".gcode.3mf", ".3mf"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name or item.label


def plan_items(db: Session, queue: Sequence[QueueItem], now: datetime) -> list[PlanItem]:
    """Bambuddy's pending and printing entries in the order of its queue. Own entries (by id, or by the
    own Bambuddy user as on the queue page) carry the article's name, others the name of their file."""
    own_ids = dispatch.own_item_ids(db)
    names = dispatch.article_names_by_item(db)
    username = st.get_setting(db, "bambuddy_username")
    found: list[PlanItem] = []
    for item in queue:
        if item.status not in ("pending", "printing"):
            continue
        own = item.id in own_ids or (bool(username) and item.created_by == username)
        found.append(PlanItem(
            item_id=item.id,
            label=names.get(item.id) or _file_label(item),
            own=own,
            status=item.status,
            printer_id=item.printer_id,
            target_model=item.target_model,
            target_location=item.target_location,
            duration=None if item.print_time_seconds is None else timedelta(seconds=item.print_time_seconds),
            manual_start=item.manual_start,
            starts_later=item.scheduled_time is not None and item.scheduled_time > now,
            been_jumped=item.been_jumped,
        ))
    return found


def plan_printers(board: PrinterBoard) -> list[PlanPrinter] | None:
    """Bambuddy's printers with their state; None while it is not known."""
    if board.printers is None:
        return None
    return [
        PlanPrinter(p.id, p.name, p.model, p.location, p.active, board.statuses.get(p.id))
        for p in board.printers
    ]


# ---- the view -----------------------------------------------------------------------------------


def _bar_view(bar: Bar, ax: Axis, now: datetime, lang: str) -> BarView:
    total = (ax.end - ax.start).total_seconds()
    left = max(0.0, min(100.0, 100 * (bar.start - ax.start).total_seconds() / total))
    right = max(0.0, min(100.0, 100 * (bar.end - ax.start).total_seconds() / total))
    parts: list[str] = []
    if bar.progress is not None:
        parts.append(f"{bar.progress:.0f} %")
    if bar.paused:
        parts.append(translate(lang, "overview.plan.paused"))
    elif not bar.duration_known:
        parts.append(translate(lang, "overview.plan.unknown_duration"))
    elif bar.kind == "running":
        parts.append(translate(lang, "overview.plan.rest", time=_span(bar.end - now)))
    cut = translate(lang, "overview.plan.cut", time=_when(bar.end, now, lang)) if bar.end > ax.end else ""
    return BarView(
        kind=bar.kind, kind_text=translate(lang, _KIND_KEYS[bar.kind]), label=bar.label, own=bar.own,
        left=round(left, 2), width=round(right - left, 2),
        start_text=_when(bar.start, now, lang), end_text=_when(bar.end, now, lang),
        detail=" · ".join(parts), cut=cut, progress=bar.progress, beyond=bar.start >= ax.end,
    )


def plan_view(
    db: Session, board: PrinterBoard, queue: Sequence[QueueItem] | None, now: datetime, lang: str, *, known: bool
) -> PlanView:
    """The print plan for the page. `known` False (Bambuddy not set up or not answering) or a board
    without printers: "Druckerzustand unbekannt"."""
    printers = plan_printers(board)
    if not known or printers is None:
        return PlanView(known=False)
    plan = build_plan(printers, plan_items(db, queue or [], now), now, shortest_first=board.shortest_first)
    ax = axis(now, plan.finish)
    lanes = tuple(
        LaneView(
            lane.name, lane.condition,
            translate(lang, _CONDITION_KEYS[lane.condition]) if lane.condition in _CONDITION_KEYS else "",
            tuple(_bar_view(bar, ax, now, lang) for bar in lane.bars), lane.waiting,
        )
        for lane in plan.lanes
    )
    stale = _clock(board.at) if board.at is not None and now - board.at > STALE_AFTER else ""
    return PlanView(
        known=True, lanes=lanes, ticks=ax.ticks,
        finish=_when(plan.finish, now, lang) if plan.finish is not None else "",
        minimum=plan.finish_is_minimum, without=plan.without, unplaced=plan.unplaced, manual=plan.manual,
        waiting_here=sum(workqueue.queued_prints(db).values()), stale=stale,
    )
