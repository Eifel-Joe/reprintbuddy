"""What the push messages report and when (Spec 10.6 "Ereignisse", "Keine Flut", "Inhalt").

After every tick `detect` brings the table `notify_marks` up to date, `due` bundles what is to be sent
into one message per kind, and `record` keeps the outcome. All three are plain functions over the
database; they make no call and wait for nothing, so the service can run them on the event loop
right after a tick (plan v0.3.12 B, D3).

Two sorts of events:
- **Once per print** (1 `print_done`, 2 `print_failed`): a mark per print. Whatever the outcome, it
  is sent once: Spec 10.6 "nach einem Fehlschlag werden Ereignisse 1 und 2 nicht nachgeholt".
  Only prints finished after the messages were switched on and within the last 7 days count
  (D1), and their marks go after 30 days (D2): a removed mark can never report its print again.
- **States** (3 `paused`, 4 `below_min`, 5 `reorder`, 6 `trouble`, 7 `rights`): a mark per thing
  while the state holds. It stays `due` until Home Assistant accepted it; any other outcome,
  "unclear" (a timeout) included, leaves it `due` for the next tick. So a message whose answer was
  unclear can arrive twice: Spec 10.6 "Senden und Fehler", ruling of the main session on plan
  v0.3.12 B. When the state has settled the mark goes, so it reports again when it comes back.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain import filament, reorder, rules, workqueue
from reprintbuddy.domain.engine import EngineStatus, collect_warnings
from reprintbuddy.domain.status import article_status
from reprintbuddy.i18n import translate, translate_count
from reprintbuddy.models import Article, Dispatch, FilamentSort, NotifyMark, Part, ReorderMark
from reprintbuddy.notify.homeassistant import Message
from reprintbuddy.web.engine_view import BAD_WARNINGS

ONCE_PER_PRINT = ("print_done", "print_failed")
PRINT_STATUS = {"print_done": "completed", "print_failed": "failed"}
PRINT_WINDOW = timedelta(days=7)  # D1: older prints never report
MARK_LIFETIME = timedelta(days=30)  # D2: marks of prints go after this
REORDER_SETTLES = timedelta(days=7)  # Spec 10.6: a suggestion gone this long has settled
TROUBLE_QUERIES = 2  # Spec 10.6: a trouble seen at two regular queries in a row
USER_PAUSE = "queue_reduced"  # the one pause the user causes (Spec 7.2, D7)
TROUBLE_PREFIX = "warning."
RIGHTS_REF = "bambuddy"


@dataclass(frozen=True)
class Rights:
    """The check of the Bambuddy key at start (Spec 11.3): `unknown` until it has run."""

    status: Literal["unknown", "ok", "missing"]
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class Context:
    now: datetime  # naive UTC, as stored
    poll: bool  # a regular tick (Spec 7.6)
    status: EngineStatus
    filament_known: bool  # this regular tick read the spool list (the hook "filament" ran)
    rights: Rights
    base: str | None  # start of the links (Spec 8.4), None: no link


@dataclass(frozen=True)
class Outgoing:
    kind: str
    mark_ids: tuple[int, ...]
    message: Message


@dataclass
class _Config:
    lang: str
    names: bool
    events: list[str]
    since: datetime  # naive UTC
    # Every mark of a state, but of the prints only the due ones: those of the last 30 days are
    # many and only their keys are needed (`printed`). Measured for condition 2 of the approval.
    marks: dict[tuple[str, str], NotifyMark] = field(default_factory=dict)
    printed: set[tuple[str, str]] = field(default_factory=set)


# ---- detect ---------------------------------------------------------------------------------------


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None) if moment.tzinfo else moment


def _config(session: Session, now: datetime) -> _Config:
    raw = st.get_setting(session, "ha_since")
    if raw is None:  # switched on before the switch kept its time, or restored: from now on
        st.set_setting(session, "ha_since", now.replace(tzinfo=UTC).isoformat(), commit=False)
        since = now
    else:
        since = _naive_utc(datetime.fromisoformat(str(raw)))
    marks = {
        (m.kind, m.ref): m
        for m in session.scalars(
            select(NotifyMark).where(or_(NotifyMark.kind.not_in(ONCE_PER_PRINT), NotifyMark.state == "due"))
        )
    }
    printed = {
        (kind, ref)
        for kind, ref in session.execute(
            select(NotifyMark.kind, NotifyMark.ref).where(NotifyMark.kind.in_(ONCE_PER_PRINT))
        )
    }
    return _Config(
        lang=str(st.get_setting(session, "language")),
        names=bool(st.get_setting(session, "ha_names")),
        events=list(st.get_setting(session, "ha_events")),  # type: ignore[arg-type]
        since=since,
        marks=marks,
        printed=printed,
    )


def _add(session: Session, cfg: _Config, kind: str, ref: str, now: datetime, state: str = "due") -> NotifyMark:
    mark = NotifyMark(kind=kind, ref=ref, state=state, streak=0, first_seen_at=now)
    session.add(mark)
    cfg.marks[(kind, ref)] = mark
    return mark


def _drop(session: Session, cfg: _Config, mark: NotifyMark) -> None:
    session.delete(mark)
    # At once: the unit of work writes inserts before deletes, and a state that settles and comes
    # back in the same tick would otherwise meet its own old row (unique kind and ref).
    session.flush()
    cfg.marks.pop((mark.kind, mark.ref), None)


def _of_kind(cfg: _Config, kind: str) -> list[NotifyMark]:
    return [m for (k, _), m in cfg.marks.items() if k == kind]


def _follow_states(session: Session, cfg: _Config, kind: str, present: Iterable[str], now: datetime) -> None:
    """A mark for every thing in the state; marks of things no longer in it go (settled)."""
    current = set(present)
    for mark in _of_kind(cfg, kind):
        if mark.ref not in current:
            _drop(session, cfg, mark)
    for ref in sorted(current):
        if (kind, ref) not in cfg.marks:
            _add(session, cfg, kind, ref, now)


def _detect_prints(session: Session, cfg: _Config, ctx: Context) -> None:
    start = max(cfg.since, ctx.now - PRINT_WINDOW)
    for kind in ONCE_PER_PRINT:
        ids = session.scalars(
            select(Dispatch.id).where(Dispatch.status == PRINT_STATUS[kind], Dispatch.finished_at >= start)
        ).all()
        # Unticked: the print is passed over for good, so ticking the box later sends nothing old.
        state = "due" if kind in cfg.events else "sent"
        for dispatch_id in ids:
            key = (kind, str(dispatch_id))
            if key not in cfg.printed:
                cfg.printed.add(key)
                _add(session, cfg, kind, key[1], ctx.now, state)
                if state != "due":
                    cfg.marks.pop(key)  # only due prints are kept in `marks`
    old = ctx.now - MARK_LIFETIME
    session.flush()
    session.execute(
        delete(NotifyMark).where(NotifyMark.kind.in_(ONCE_PER_PRINT), NotifyMark.first_seen_at < old)
    )
    for key, mark in list(cfg.marks.items()):
        if key[0] in ONCE_PER_PRINT and mark.first_seen_at < old:
            cfg.marks.pop(key)


def _paused_refs(session: Session) -> list[str]:
    ids = session.scalars(
        select(Article.id).where(
            Article.paused.is_(True),
            Article.archived_at.is_(None),
            (Article.pause_reason.is_(None)) | (Article.pause_reason != USER_PAUSE),
        )
    ).all()
    return [str(i) for i in ids]


RED = ("below_min", "overbooked")


def red_articles(session: Session) -> dict[int, str]:
    """Active articles in a red state (Spec 10.1), by id. After the tick, so after rule 7.1: "low"
    (yellow, a reprint on its way) is no event (D8).

    The same numbers and rules as `catalog.article_row` (the overview), but over the few columns it
    needs: `list_articles` loads every article whole, thumbnail included, which took most of the time
    of a tick's detection with 500 articles (condition 2 of the plan's approval, measured in the
    report). tests/notify/test_events.py checks that both agree.
    """
    queued = workqueue.queued_prints(session)
    in_progress = workqueue.in_progress_units(session)
    rows = session.execute(
        select(
            Article.id, Article.stock, Article.reserved, Article.min_stock, Article.units_per_print,
            Article.provisional, Article.awaiting_assembly,
        ).where(Article.archived_at.is_(None))
    )
    red: dict[int, str] = {}
    for row in rows:
        available = rules.available(row.stock, row.reserved)
        coverage = rules.coverage(
            available, queued.get(row.id, 0) * row.units_per_print, in_progress.get(row.id, 0),
            row.provisional, row.awaiting_assembly,
        )
        state = article_status(available=available, min_stock=row.min_stock, coverage=coverage)
        if state in RED:
            red[row.id] = state
    return red


def _detect_reorder(session: Session, cfg: _Config, ctx: Context) -> None:
    """Spec 10.6 event 5 and D4, D5: at regular queries only; unknown filament stock settles nothing."""
    sorts = filament.known_sorts(session, None)[0] if ctx.filament_known else None
    found = reorder.suggestions(session, sorts=sorts, today=ctx.now.date())
    current = {f"{s.item_type}:{s.item_id}" for s in found}
    received: dict[str, datetime] = {}
    open_marks: set[str] = set()
    for item_type, item_id, received_at, withdrawn in session.execute(
        select(ReorderMark.item_type, ReorderMark.item_id, ReorderMark.received_at, ReorderMark.withdrawn)
    ):
        ref = f"{item_type}:{item_id}"
        if received_at is None:
            open_marks.add(ref)
        elif withdrawn:
            continue  # Spec 10.6 (p280): an order taken back settles nothing, nothing arrived
        elif ref not in received or received_at > received[ref]:
            received[ref] = received_at
    for mark in _of_kind(cfg, "reorder"):
        if mark.ref.startswith("filament:") and sorts is None:
            continue  # stock unknown: nothing settles
        if mark.ref in received and received[mark.ref] > mark.first_seen_at:
            _drop(session, cfg, mark)  # ordered and received: settled
        elif mark.ref in current or mark.ref in open_marks:
            mark.gone_since = None  # still there, or ordered and on its way
        elif mark.gone_since is None:
            mark.gone_since = ctx.now
        elif ctx.now - mark.gone_since >= REORDER_SETTLES:
            _drop(session, cfg, mark)
    for ref in sorted(current):
        if ("reorder", ref) not in cfg.marks:
            _add(session, cfg, "reorder", ref, ctx.now)


def _detect_trouble(session: Session, cfg: _Config, ctx: Context) -> None:
    """Spec 10.6 event 6, D6: a red warning seen at two regular queries in a row."""
    present = {
        w.key.removeprefix(TROUBLE_PREFIX)
        for w in collect_warnings(session, ctx.status)
        if w.key in BAD_WARNINGS
    }
    for mark in _of_kind(cfg, "trouble"):
        if mark.ref not in present:
            _drop(session, cfg, mark)
    for ref in sorted(present):
        mark = cfg.marks.get(("trouble", ref)) or _add(session, cfg, "trouble", ref, ctx.now, "watch")
        mark.streak += 1
        if mark.state == "watch" and mark.streak >= TROUBLE_QUERIES:
            mark.state = "due"


def detect(session: Session, ctx: Context) -> _Config:
    """Bring the marks up to date after a tick (in the caller's write transaction). The result can
    be handed to `due` in the same transaction, which then does not read the marks again."""
    cfg = _config(session, ctx.now)
    _detect_prints(session, cfg, ctx)
    for kind in ("paused", "below_min", "reorder", "trouble", "rights"):
        if kind not in cfg.events:
            # An unticked state is not followed; ticked again, what holds then reports once.
            for mark in _of_kind(cfg, kind):
                _drop(session, cfg, mark)
    if "paused" in cfg.events:
        _follow_states(session, cfg, "paused", _paused_refs(session), ctx.now)
    if "below_min" in cfg.events:
        _follow_states(session, cfg, "below_min", [str(i) for i in red_articles(session)], ctx.now)
    if "reorder" in cfg.events and ctx.poll:
        _detect_reorder(session, cfg, ctx)
    if "trouble" in cfg.events and ctx.poll:
        _detect_trouble(session, cfg, ctx)
    if "rights" in cfg.events and ctx.rights.status != "unknown":
        _follow_states(session, cfg, "rights", [RIGHTS_REF] if ctx.rights.status == "missing" else [], ctx.now)
    session.flush()
    return cfg


# ---- due: one message per kind --------------------------------------------------------------------


def _link(ctx: Context, path: str) -> str | None:
    return f"{ctx.base.rstrip('/')}{path}" if ctx.base else None


def _article_link(ctx: Context, article_ids: list[int]) -> str | None:
    """The article's page for one article, else the overview (Spec 10.6 "Inhalt")."""
    unique = sorted(set(article_ids))
    return _link(ctx, f"/articles/{unique[0]}" if len(unique) == 1 else "/")


def _text(lang: str, kind: str, names: bool, n: int, items: list[str]) -> str:
    if names and items:
        return translate_count(lang, f"notify.{kind}.named", n, items=", ".join(items))
    return translate_count(lang, f"notify.{kind}.count", n)


def _message(session: Session, ctx: Context, cfg: _Config, kind: str, marks: list[NotifyMark]) -> Message:
    lang, names = cfg.lang, cfg.names
    title = translate(lang, f"notify.{kind}.title")
    n = len(marks)
    if kind in ONCE_PER_PRINT:
        rows = session.execute(
            select(Dispatch.article_id, Dispatch.units, Article.name)
            .join(Article, Article.id == Dispatch.article_id)
            .where(Dispatch.id.in_([int(m.ref) for m in marks]))
            .order_by(Article.name, Dispatch.id)
        ).all()
        if kind == "print_done":
            items = [translate(lang, "notify.units", name=r.name, units=r.units) for r in rows]
            return Message(title, _text(lang, kind, names, n, items), _link(ctx, "/inspection"))
        items = [r.name for r in rows]
        return Message(title, _text(lang, kind, names, n, items), _article_link(ctx, [r.article_id for r in rows]))
    if kind in ("paused", "below_min"):
        articles = session.scalars(
            select(Article).where(Article.id.in_([int(m.ref) for m in marks])).order_by(Article.name, Article.id)
        ).all()
        if kind == "paused":
            items = [
                translate(
                    lang, "notify.paused.item", name=a.name,
                    reason=translate(lang, f"notify.reason.{a.pause_reason or 'other'}"),
                )
                for a in articles
            ]
        else:
            overbooked = {i for i, state in red_articles(session).items() if state == "overbooked"}
            items = [
                translate(lang, "notify.below_min.overbooked", name=a.name) if a.id in overbooked else a.name
                for a in articles
            ]
        return Message(title, _text(lang, kind, names, n, items), _article_link(ctx, [a.id for a in articles]))
    if kind == "reorder":
        labels = []
        for mark in marks:
            item_type, item_id = mark.ref.split(":", 1)
            model = FilamentSort if item_type == "filament" else Part
            thing = session.get(model, int(item_id))
            if thing is not None:
                labels.append(thing.label if item_type == "filament" else thing.name)
        return Message(title, _text(lang, kind, names, n, sorted(labels, key=str.casefold)), _link(ctx, "/reorder"))
    if kind == "trouble":
        text = " ".join(translate(lang, f"notify.trouble.{m.ref}") for m in marks)
        return Message(title, text, _link(ctx, "/"))
    # rights: the names of rights are no names of things, they show either way
    text = translate(lang, "notify.rights.text", rights=", ".join(ctx.rights.missing))
    return Message(title, text, _link(ctx, "/"))


def due(
    session: Session, ctx: Context, *, exclude: frozenset[int], cfg: _Config | None = None
) -> list[Outgoing]:
    """The messages to send now: one per ticked kind, in the order of Spec 10.6. `cfg`: what
    `detect` returned in the same transaction."""
    cfg = cfg if cfg is not None else _config(session, ctx.now)
    result: list[Outgoing] = []
    for kind in st.HA_EVENTS:
        if kind not in cfg.events:
            continue
        marks = sorted(
            (m for m in _of_kind(cfg, kind) if m.state == "due" and m.id not in exclude),
            key=lambda m: m.id,
        )
        if kind == "rights" and ctx.rights.status != "missing":
            marks = []
        if marks:
            message = _message(session, ctx, cfg, kind, marks)
            result.append(Outgoing(kind, tuple(m.id for m in marks), message))
    return result


# ---- record ---------------------------------------------------------------------------------------


def record(session: Session, out: Outgoing, outcome: str, now: datetime) -> None:
    """Keep the outcome: accepted marks are sent; a print is never tried again; a state stays due."""
    accepted = outcome == "accepted"
    if not accepted and out.kind not in ONCE_PER_PRINT:
        return
    for mark in session.scalars(select(NotifyMark).where(NotifyMark.id.in_(out.mark_ids))):
        mark.state = "sent"
        if accepted:
            mark.notified_at = now


def forget_all(session: Session) -> None:
    """Switched off: nothing is followed any more (switched on again, `ha_since` starts anew)."""
    session.execute(delete(NotifyMark))
