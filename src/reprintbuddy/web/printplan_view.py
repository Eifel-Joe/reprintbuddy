"""The print plan of the overview laid out for the page (Spec 10.2, 0.4.2): time axis, bars, texts.

The assignment itself is `domain.printplan.build_plan`; this module feeds it from the printers' state in
memory (`PrinterBoard`, Spec 7.6) and Bambuddy's queue as the last query read it (`EngineStatus.bambuddy_queue`),
and turns its times into positions on the axis and local clock times. Nothing here asks Bambuddy.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain import dispatch
from reprintbuddy.domain.printerboard import PrinterBoard
from reprintbuddy.bambuddy.types import AmsSlot, QueueItem
from reprintbuddy.domain.filamentplan import LaneFilament, LanePrint, Marker, Need, lane_filament
from reprintbuddy.domain.printplan import Bar, Plan, PlanItem, PlanPrinter, build_plan
from reprintbuddy.localtime import to_local
from reprintbuddy.domain.workqueue import OPEN_STATES
from reprintbuddy.domain import color as color_mod
from reprintbuddy.models import Article, ArticleFilament, Dispatch, FilamentSort, WorkQueueEntry
from reprintbuddy.web.i18n import translate

MIN_SPAN = timedelta(hours=2)  # the axis reaches from now to "alles fertig", at least 2 h (Spec 10.2)
MAX_SPAN = timedelta(hours=24)  # and at most 24 h; a bar beyond ends at the edge with "→ bis …"
STALE_AFTER = timedelta(minutes=5)  # "Stand HH:MM" when the printers' state is older (Spec 10.2)

_KIND_KEYS = {
    "running": "overview.plan.kind_running",
    "fixed": "overview.plan.kind_fixed",
    "planned": "overview.plan.kind_planned",
    "backlog": "overview.plan.kind_backlog",
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
class MarkerView:
    """A filament note of a lane (p648): "Reload about 14:30 · PLA #000000 (AMS 1, slot 2)" and its place on
    the axis (percent), None without a time or beyond the axis."""

    kind: str  # reload, slot_empty (p716), not_loaded, unknown_stock
    text: str
    left: float | None
    color: str | None  # "#RRGGBB" for the swatch


@dataclass(frozen=True)
class LaneView:
    name: str
    condition: str
    condition_text: str
    bars: tuple[BarView, ...]
    waiting: tuple[str, ...]
    markers: tuple[MarkerView, ...] = ()  # the filament of the printer (p648), at most three
    more: int = 0  # further markers: "+ n"


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
    held: int = 0  # prints of Reprintbuddy's queue the hand-over skips: held entries, paused articles (p646)
    no_slot: int = 0  # prints of Reprintbuddy's queue that never get a place under the limit
    stopped: int = 0  # prints of Reprintbuddy's stopped queue (held ones too); then none is planned
    reload: str = ""  # "Reload needed: Printer 1 about 14:30 and 2 more" (p648)
    filament_short: int = 0  # entries Bambuddy held at their start for too little filament
    stale: str = ""  # local time of the printers' state when older than 5 minutes


# ---- times --------------------------------------------------------------------------------------


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
    replacement_ids = dispatch.own_replacement_item_ids(db)  # Spec 10.2 "Ersatzdrucke vorn" (1.2.2)
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
            nozzle=item.nozzle_diameter,
            holds_slot=item.id in own_ids,
            replacement=item.id in replacement_ids,
        ))
    return found


@dataclass
class Backlog:
    """Reprintbuddy's own queue as the print plan takes it (Spec 10.2, p646)."""

    items: list[PlanItem] = field(default_factory=list)  # one per print, top down, as the hand-over takes them
    parked: list[tuple[int, str]] = field(default_factory=list)  # (printer, article) skipped for maintenance
    held: int = 0
    unplaced: int = 0  # skipped because every printer of the model is in maintenance
    stopped: int = 0
    free_slots: int = 0
    insert_next: bool = False


def backlog(db: Session, board: PrinterBoard, waiting_for_printer: Collection[int]) -> Backlog:
    """The prints of Reprintbuddy's queue under the rules of the hand-over (`dispatch.reserve_next`,
    `engine._hand_over`): top down, print by print; held entries and paused, archived or target-less
    articles are skipped (counted as held), an article whose printers are in maintenance
    (`waiting_for_printer`) is skipped too: in the lane of its own printer, or, with a model, counted
    without a ready printer. A stopped queue hands over nothing. Duration and nozzle come from the
    files' facts the regular query read (`engine._read_file_facts`); unknown until then."""
    rows = db.execute(
        select(WorkQueueEntry, Article)
        .join(Article, Article.id == WorkQueueEntry.article_id)
        .order_by(WorkQueueEntry.position, WorkQueueEntry.id)
    ).all()
    if not st.get_setting(db, "queue_running"):
        return Backlog(stopped=sum(entry.prints for entry, _article in rows))
    found = Backlog(
        free_slots=int(st.get_setting(db, "queue_cap")) - dispatch.open_count(db),
        insert_next=bool(st.get_setting(db, "insert_next")),
    )
    skip = set(waiting_for_printer)
    for entry, article in rows:
        if entry.held or article.paused or article.archived_at is not None or (
            not article.target_model and article.printer_id is None
        ):
            found.held += entry.prints
            continue
        if article.id in skip:
            if article.printer_id is not None:
                found.parked.extend([(article.printer_id, article.name)] * entry.prints)
            else:
                found.unplaced += entry.prints
            continue
        facts = board.file_facts.get((article.library_file_id, article.file_fingerprint))
        seconds = facts.plate_seconds.get(article.plate_id) if facts is not None else None
        item = PlanItem(
            item_id=0,  # not in Bambuddy yet
            label=article.name,
            own=True,
            status="pending",
            printer_id=article.printer_id,  # the chosen printer wins over the model (`dispatch._reserve`)
            target_model=None if article.printer_id is not None else article.target_model,
            target_location=None,
            duration=None if seconds is None else timedelta(seconds=seconds),
            manual_start=False,
            starts_later=False,
            nozzle=facts.nozzle if facts is not None else None,
            replacement=entry.origin in dispatch.REPLACEMENT_ORIGINS,  # to the top of Bambuddy's queue (1.2.2)
            ref=article.id,  # the reload hint finds its filament through the article (p648)
        )
        found.items.extend([item] * entry.prints)
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


def _bar_view(bar: Bar, ax: Axis, now: datetime, lang: str, *, need_unknown: bool = False) -> BarView:
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
    if need_unknown:
        parts.append(translate(lang, "overview.plan.need_unknown"))
    cut = translate(lang, "overview.plan.cut", time=_when(bar.end, now, lang)) if bar.end > ax.end else ""
    return BarView(
        kind=bar.kind, kind_text=translate(lang, _KIND_KEYS[bar.kind]), label=bar.label, own=bar.own,
        left=round(left, 2), width=round(right - left, 2),
        start_text=_when(bar.start, now, lang), end_text=_when(bar.end, now, lang),
        detail=" · ".join(parts), cut=cut, progress=bar.progress, beyond=bar.start >= ax.end,
    )


def _plan(
    db: Session, board: PrinterBoard, queue: Sequence[QueueItem] | None, now: datetime, *, known: bool,
    waiting_for_printer: Collection[int],
) -> tuple[Plan, Backlog] | None:
    """The one calculation behind the overview's plan and the queue page's times (Spec 10.3a); None while
    the printers' state is not known."""
    printers = plan_printers(board)
    if not known or printers is None:
        return None
    own = backlog(db, board, waiting_for_printer)
    plan = build_plan(
        printers, plan_items(db, queue or [], now), now, shortest_first=board.shortest_first,
        backlog=own.items, free_slots=own.free_slots, insert_next=own.insert_next, parked=own.parked,
    )
    return plan, own


def plan_view(
    db: Session, board: PrinterBoard, queue: Sequence[QueueItem] | None, now: datetime, lang: str, *, known: bool,
    live: bool = False, waiting_for_printer: Collection[int] = (),
) -> PlanView:
    """The print plan for the page. `known` False (Bambuddy not set up or not answering) or a board
    without printers: "Druckerzustand unbekannt". `live`: Bambuddy's live channel is connected; it reports
    every change of a printer, so the state is current however old its last message is (no "as of")."""
    built = _plan(db, board, queue, now, known=known, waiting_for_printer=waiting_for_printer)
    if built is None:
        return PlanView(known=False)
    plan, own = built
    ax = axis(now, plan.finish)
    filament = _filament(db, board, queue or [], plan, now)
    names = color_names(db) if filament else {}
    lanes = tuple(
        LaneView(
            lane.name, lane.condition,
            translate(lang, _CONDITION_KEYS[lane.condition]) if lane.condition in _CONDITION_KEYS else "",
            tuple(_bar_view(bar, ax, now, lang, need_unknown=n in filament[lane.printer_id].unknown_needs)
                  for n, bar in enumerate(lane.bars)) if lane.printer_id in filament
            else tuple(_bar_view(bar, ax, now, lang) for bar in lane.bars),
            lane.waiting,
            *_markers(filament.get(lane.printer_id), ax, now, lang, names),
        )
        for lane in plan.lanes
    )
    stale = _when(board.at, now, lang) if board.at is not None and not live and now - board.at > STALE_AFTER else ""
    return PlanView(
        known=True, lanes=lanes, ticks=ax.ticks,
        finish=_when(plan.finish, now, lang) if plan.finish is not None else "",
        minimum=plan.finish_is_minimum, without=plan.without, unplaced=plan.unplaced + own.unplaced,
        manual=plan.manual, held=own.held, no_slot=plan.no_slot, stopped=own.stopped, stale=stale,
        reload=_reload_line(plan, filament, now, lang),
        filament_short=sum(1 for i in queue or [] if i.status == "pending" and i.filament_short),
    )


# ---- filament in the printers (Spec 10.2, p648) ------------------------------------------------


def slot_name(lang: str, slot: AmsSlot) -> str:
    """"AMS 1, slot 2", "AMS-HT 1", "external spool" (a second one numbered), as Bambuddy stores the slot."""
    if slot.ams_id == 255:
        if slot.tray_id == 0:
            return translate(lang, "overview.plan.slot_external")
        return translate(lang, "overview.plan.slot_external_n", n=slot.tray_id + 1)
    if slot.ams_id >= 128:
        return translate(lang, "overview.plan.slot_ht", n=slot.ams_id - 127)
    return translate(lang, "overview.plan.slot_ams", unit=slot.ams_id + 1, slot=slot.tray_id + 1)


ColorNames = dict[tuple[str, tuple[int, int, int]], str | None]


def color_names(db: Session) -> ColorNames:
    """(material, RGB) -> the colour name of Reprintbuddy's spool sorts (Nachtrag p648), as the filament page
    and the article page show it. Two different names for one material and value: None (the value is shown);
    legacy sorts show nowhere and are left out."""
    found: dict[tuple[str, tuple[int, int, int]], set[str]] = {}
    for material, value, name in db.execute(
        select(FilamentSort.material, FilamentSort.color_hex, FilamentSort.color_name)
        .where(FilamentSort.legacy.is_(False))
    ):
        rgb = color_mod.parse_hex(value)
        if rgb is None:
            continue
        names = found.setdefault(((material or "").strip().casefold(), rgb), set())
        if name and name.strip():
            names.add(name.strip())
    return {key: next(iter(names)) if len(names) == 1 else None for key, names in found.items()}


def _sort(lang: str, material: str | None, color: str | None, names: ColorNames | None = None) -> str:
    """"PLA Black" when a spool sort names the colour, else "PLA #000000" (`color.display_hex`)."""
    rgb = color_mod.parse_hex(color)
    name = (names or {}).get(((material or "").strip().casefold(), rgb)) if rgb is not None else None
    shown = name or (color_mod.display_hex(color) if color else None)
    text = " ".join(part for part in (material, shown) if part)
    return text or translate(lang, "overview.plan.sort_unknown")


_HEX_COLOR = re.compile(r"^#[0-9A-F]{6}$")  # the only form a colour may take in the style attribute


def _needs_of_articles(db: Session, article_ids: Collection[int]) -> dict[int, tuple[Need, ...] | None]:
    found: dict[int, list[Need]] = {}
    if article_ids:
        for row in db.scalars(
            select(ArticleFilament).where(ArticleFilament.article_id.in_(article_ids)).order_by(ArticleFilament.slot)
        ):
            found.setdefault(row.article_id, []).append(Need(row.material, row.color_hex, row.grams))
    # an article without filament rows: the need is not known
    return {a: tuple(found[a]) if a in found else None for a in article_ids}


def _filament(
    db: Session, board: PrinterBoard, queue: Sequence[QueueItem], plan: Plan, now: datetime,
) -> dict[int, LaneFilament]:
    """Per lane in service that reports its slots: whether the filament lasts (`domain.filamentplan`). The
    need of a print: own (handed over or still in Reprintbuddy) from the article's print file, foreign from
    Bambuddy's file manager as the last query read it; a running print without a queue entry is unknown."""
    items = {i.id: i for i in queue}
    articles_by_item = dict(db.execute(
        select(Dispatch.bambuddy_item_id, Dispatch.article_id)
        .where(Dispatch.status.in_(OPEN_STATES), Dispatch.bambuddy_item_id.is_not(None))
    ).all())
    wanted = {bar.ref for lane in plan.lanes for bar in lane.bars if bar.ref is not None}
    wanted |= {articles_by_item[b.item_id] for lane in plan.lanes for b in lane.bars if b.item_id in articles_by_item}
    article_needs = _needs_of_articles(db, wanted)
    # Spec 10.2 "Reserve" (p720): the grams per sort that count as used up; the switch off reckons to 0 g.
    reserve = float(st.get_setting(db, "plan_reserve_grams")) if st.get_setting(db, "plan_reserve") else 0.0  # type: ignore[arg-type]
    found: dict[int, LaneFilament] = {}
    for lane in plan.lanes:
        state = board.statuses.get(lane.printer_id)
        # Review p648, 2: Bambuddy sends empty lists for a printer without AMS (schema defaults), so "no slots"
        # is an empty tuple as well as None; either way the lane gets no notes (Spec 10.2).
        if lane.condition not in ("ready", "plate") or state is None or not state.slots or not lane.bars:
            continue
        prints: list[LanePrint] = []
        for bar in lane.bars:
            item = items.get(bar.item_id) if bar.item_id is not None else None
            if bar.ref is not None:
                needs = article_needs.get(bar.ref)
            elif bar.item_id in articles_by_item:
                needs = article_needs.get(articles_by_item[bar.item_id])
            elif item is not None and item.library_file_id is not None and item.plate_id is not None:
                known = board.needs.get((item.library_file_id, item.plate_id))
                needs = None if known is None else tuple(Need(r.material, r.color_hex, r.grams) for r in known)
            else:
                needs = None
            start = bar.start
            if bar.kind == "running" and item is not None and item.print_time_seconds:
                start = bar.end - timedelta(seconds=item.print_time_seconds)  # the whole print, for the share
            elif bar.kind == "running" and bar.progress and 0 < bar.progress < 100:
                # no print time: the part done so far, from the progress and the time left (review p648, 9)
                start = now - (bar.end - now) * (bar.progress / (100 - bar.progress))
            prints.append(LanePrint(bar.start, bar.end, start, needs))
        found[lane.printer_id] = lane_filament(
            prints, state.slots, board.slot_grams.get(lane.printer_id, {}), state.backup, now,
            prefer_lowest=board.prefer_lowest, reserve=reserve,
        )
    return found


def _slot_empty_text(lang: str, marker: Marker, now: datetime) -> str:
    """"AMS 1, slot 3 empty about 10:30, continuing with AMS 1, slot 2" (Spec 10.2 "Nachladen", p716): the
    slots named as in the other notes, so the sentence may begin with "external spool"; its first letter is
    made a capital."""
    where = slot_name(lang, marker.slot) if marker.slot is not None else ""
    following = slot_name(lang, marker.next_slot) if marker.next_slot is not None else ""
    if marker.due:
        text = translate(lang, "overview.plan.slot_empty_now", slot=where, next=following)
    else:
        key = "overview.plan.slot_empty_latest" if marker.latest else "overview.plan.slot_empty"
        text = translate(lang, key, slot=where, time=_when(marker.at, now, lang), next=following)
    return text[:1].upper() + text[1:]


def _marker_text(lang: str, marker: Marker, now: datetime, names: ColorNames | None = None) -> str:
    if marker.kind == "slot_empty":
        return _slot_empty_text(lang, marker, now)
    sort = _sort(lang, marker.material, marker.color, names)
    if marker.kind == "not_loaded":
        return translate(lang, "overview.plan.not_loaded", sort=sort)
    where = slot_name(lang, marker.slot) if marker.slot is not None else ""
    if marker.kind == "unknown_stock":
        return translate(lang, "overview.plan.stock_unknown", sort=sort, slot=where)
    if marker.due:
        return translate(lang, "overview.plan.reload_now", sort=sort, slot=where)
    key = "overview.plan.reload_latest" if marker.latest else "overview.plan.reload"
    return translate(lang, key, time=_when(marker.at, now, lang), sort=sort, slot=where)


def _markers(
    found: LaneFilament | None, ax: Axis, now: datetime, lang: str, names: ColorNames | None = None,
) -> tuple[tuple[MarkerView, ...], int]:
    if found is None:
        return (), 0
    total = (ax.end - ax.start).total_seconds()
    views = []
    for marker in found.markers:
        left = None
        if marker.at is not None:
            moment = max(marker.at, ax.start)
            if moment <= ax.end:
                left = round(100 * (moment - ax.start).total_seconds() / total, 2)
        color = marker.color if marker.color and _HEX_COLOR.match(marker.color) else None
        views.append(MarkerView(marker.kind, _marker_text(lang, marker, now, names), left, color))
    return tuple(views), found.more


def _reload_line(plan: Plan, found: dict[int, LaneFilament], now: datetime, lang: str) -> str:
    """"Reload needed: <printer> about 14:30", the earliest of all lanes, "and n more" for the others."""
    names = {lane.printer_id: lane.name for lane in plan.lanes}
    reloads = sorted(
        ((m.at, names[pid], m) for pid, lane in found.items() for m in _all_reloads(lane)),
        key=lambda x: (x[0], x[1]),
    )
    if not reloads:
        return ""
    _at, name, first = reloads[0]
    if first.due:
        when = translate(lang, "overview.plan.now")
    else:
        key = "overview.plan.at_latest_about" if first.latest else "queue.time_about"
        when = translate(lang, key, time=_when(first.at, now, lang))
    text = translate(lang, "overview.plan.reload_needed", printer=name, when=when)
    if len(reloads) > 1:
        text += " " + translate(lang, "overview.plan.reload_more", n=len(reloads) - 1)
    return text


def _all_reloads(lane: LaneFilament) -> list[Marker]:
    shown = [m for m in lane.markers if m.kind == "reload"]
    return shown + list(lane.hidden_reloads)


# ---- the queue page: times of the prints in progress (Spec 10.3a, p647) -------------------------


@dataclass(frozen=True)
class PrintTimes:
    start: str  # "" when not in the lanes; a running print: Bambuddy's start without "about", or "running"
    end: str
    duration: str  # the slicer's print time of the file, "Duration unknown"


def in_progress_times(
    db: Session, board: PrinterBoard, queue: Sequence[QueueItem] | None, now: datetime, lang: str, *,
    known: bool, waiting_for_printer: Collection[int] = (),
) -> dict[int, PrintTimes]:
    """Per open print (`Dispatch.id`): start, end and print time, from the same plan as the overview's bars
    (Spec 10.3a). A print found in no lane - not yet in Bambuddy's list, without a ready printer, waiting
    for a start by hand or for a printer out of service, or the printers' state unknown - keeps its print
    time but no start or end. The print time comes from Bambuddy's entry, for a print not in its list from
    the file's facts of the last query (p646)."""
    built = _plan(db, board, queue, now, known=known, waiting_for_printer=waiting_for_printer)
    bars = {} if built is None else {
        bar.item_id: bar for lane in built[0].lanes for bar in lane.bars if bar.item_id is not None
    }
    items = {item.id: item for item in queue or []}
    rows = db.execute(
        select(Dispatch, Article.file_fingerprint)
        .join(Article, Article.id == Dispatch.article_id)
        .where(Dispatch.status.in_(OPEN_STATES))
    ).all()
    found: dict[int, PrintTimes] = {}
    for row, fingerprint in rows:
        item = items.get(row.bambuddy_item_id) if row.bambuddy_item_id is not None else None
        if item is not None:
            seconds = item.print_time_seconds
        else:
            facts = board.file_facts.get((row.library_file_id, fingerprint)) if row.library_file_id else None
            seconds = facts.plate_seconds.get(row.plate_id) if facts is not None and row.plate_id else None
        duration = (translate(lang, "overview.plan.unknown_duration") if seconds is None
                    else _span(timedelta(seconds=seconds)))
        bar = bars.get(row.bambuddy_item_id) if row.bambuddy_item_id is not None else None
        if bar is None:
            found[row.id] = PrintTimes("", "", duration)
            continue
        if bar.kind == "running":
            started = item.started_at if item is not None else None
            # The real start, without "about"; on another day with the weekday like every time of the plan
            # (Spec 10.3a). It was a bare clock time: a start of yesterday evening read like one later today.
            start = _when(started, now, lang) if started is not None else translate(lang, "queue.running")
        else:
            start = translate(lang, "queue.time_about", time=_when(bar.start, now, lang))
        end_key = "queue.time_about" if bar.duration_known else "queue.time_at_least"
        found[row.id] = PrintTimes(start, translate(lang, end_key, time=_when(bar.end, now, lang)), duration)
    return found
