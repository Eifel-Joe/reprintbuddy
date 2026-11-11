"""Changes to settings, the account and master data, logged where they are saved (Spec 10.7).

The listeners hang on every sessionmaker (`db.make_sessionmaker`), so the app, the automation and
the commands on the server are covered alike and no writing place can forget the log. `after_flush`
collects what changed over all flushes of a transaction; `before_commit` flushes once more, builds
the lines and adds them to the same transaction: a change that is rolled back takes its line with
it. A line that cannot be built is left out and logged; the change itself is committed anyway.

Who wrote it stands in `session.info`: `ACTOR` (user, auto; missing means system), `VIA` ("cli")
and `ADDRESS` (the device of a request). `acting()` marks what Reprintbuddy changes by itself,
`quiet()` leaves out what belongs in the journal (a new print file, a converted spool sort).

Only the columns of the edit forms count as master data (`changes.SETTINGS_COLUMNS`); stock, pause
and print file are the journal's. Rows that are deleted and written again with the same content
(the parts of an article, the base parts and choices of a set) are no change.
"""

from __future__ import annotations

import json
import logging
import weakref
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy import settings as st
from reprintbuddy.activity import values
from reprintbuddy.activity.kinds import ACTORS, area_of
from reprintbuddy.models import (
    ActivityEntry,
    Article,
    ArticleFilamentSort,
    ArticlePart,
    FilamentSettings,
    FilamentSupplier,
    FilamentSort,
    Part,
    PartSupplier,
    ProductSet,
    ReorderMark,
    SetPosition,
    SetPositionOption,
    Setting,
    User,
)

log = logging.getLogger(__name__)

ACTOR = "activity_actor"
VIA = "activity_via"
ADDRESS = "activity_address"
_QUIET = "activity_quiet"
_DATA = "activity_data"

# The columns of the edit forms (Design 4.6); the same lists tell the open forms that they changed.
ARTICLE_COLUMNS: tuple[str, ...] = (
    "name", "sku", "label_text", "units_per_print", "min_stock", "buffer",
    "target_model", "printer_id", "needs_assembly",
)
PART_COLUMNS: tuple[str, ...] = ("name", "unit", "min_stock", "safety_value", "safety_unit")
SET_COLUMNS: tuple[str, ...] = ("name", "sku")
FILAMENT_COLUMNS: tuple[str, ...] = ("safety_value", "safety_unit")  # the rest per supplier (0016)
SUPPLIER_COLUMNS: tuple[str, ...] = (
    "position", "name", "order_url", "pack_size", "min_order_packs", "lead_time_days", "is_default",
)


@dataclass
class _Thing:
    """What changed of one article, part or set in this transaction."""

    actor: str
    created: bool = False
    fields: set[str] = field(default_factory=set)
    archived: bool | None = None  # True archived, False restored
    auto: bool | None = None  # the new state of the article's own automation switch
    old_rows: dict[str, Counter] = field(default_factory=dict)  # child rows deleted, by field
    new_rows: dict[str, Counter] = field(default_factory=dict)  # child rows added, by field


@dataclass
class _Data:
    settings: dict[str, list[Any]] = field(default_factory=dict)  # key -> [old, new, actor]
    account_created: str | None = None  # actor
    credentials_changed: str | None = None
    things: dict[tuple[str, int], _Thing] = field(default_factory=dict)
    filament: dict[int, str] = field(default_factory=dict)  # sort id -> actor
    ordered: list[tuple[int, str]] = field(default_factory=list)  # mark id, actor
    received: list[tuple[int, str]] = field(default_factory=list)
    withdrawn: list[tuple[int, str]] = field(default_factory=list)  # p280: taken back
    arrived: dict[int, float] = field(default_factory=dict)  # mark id -> what had arrived when taken back
    positions: dict[int, int] = field(default_factory=dict)  # position id -> set id
    meta: dict[str, Any] = field(default_factory=dict)  # via and address of the request


def _data(session: Session) -> _Data:
    data = session.info.get(_DATA)
    if data is None:
        data = session.info[_DATA] = _Data()
    return data


def _actor(session: Session) -> str:
    """Who writes; anything but a known actor is the system (the table allows only those)."""
    actor = session.info.get(ACTOR)
    return actor if actor in ACTORS else "system"


@contextmanager
def acting(session: Session, actor: str) -> Iterator[None]:
    """Changes in this block are made by `actor` (e.g. "auto": Reprintbuddy by its rules, Spec 9.4)."""
    session.flush()
    before = session.info.get(ACTOR)
    session.info[ACTOR] = actor
    try:
        yield
        session.flush()
    finally:
        session.info[ACTOR] = before


@contextmanager
def quiet(session: Session) -> Iterator[None]:
    """Changes in this block are not logged: they belong in the journal (Spec 10.7, 6.3)."""
    session.flush()
    before = session.info.get(_QUIET, False)
    session.info[_QUIET] = True
    try:
        yield
        session.flush()
    finally:
        session.info[_QUIET] = before


def _history(obj: object, name: str) -> tuple[bool, object]:
    """(changed, value before) of one column of a dirty row."""
    hist = inspect(obj).attrs[name].history
    if not hist.has_changes():
        return False, None
    return True, hist.deleted[0] if hist.deleted else None


def _thing(data: _Data, kind: str, ident: int, actor: str) -> _Thing:
    key = (kind, ident)
    thing = data.things.get(key)
    if thing is None:
        thing = data.things[key] = _Thing(actor=actor)
    return thing


def _set_of_position(session: Session, data: _Data, position_id: int) -> int | None:
    if position_id in data.positions:
        return data.positions[position_id]
    position = session.get(SetPosition, position_id)
    if position is None:
        return None
    data.positions[position_id] = position.set_id
    return position.set_id


def _collect_setting(data: _Data, obj: Setting, change: str, actor: str) -> None:
    key = obj.key
    if key not in values.SETTINGS:
        return  # internal, or unknown to this version
    new = None if change == "deleted" else json.loads(obj.value)
    if change == "new":
        old = st.DEFAULTS.get(key)
    elif change == "deleted":
        old = json.loads(obj.value)
    else:
        changed, before = _history(obj, "value")
        if not changed:
            return
        old = json.loads(before) if isinstance(before, str) else st.DEFAULTS.get(key)
    if key in data.settings:
        data.settings[key][1:] = [new, actor]  # the first old value of the transaction counts
    else:
        data.settings[key] = [old, new, actor]


def _differs_from_defaults(obj: FilamentSettings | FilamentSupplier) -> bool:
    """A first saving that keeps every default changes nothing (Spec 10.7: "wirklich etwas
    unterscheidet"): the settings and one supplier as the form starts them."""
    if isinstance(obj, FilamentSupplier):
        names, defaults = SUPPLIER_COLUMNS, {"position": 0, "is_default": True}
    else:
        names, defaults = FILAMENT_COLUMNS, {}
    columns = type(obj).__table__.c
    for name in names:
        if name in defaults:
            default = defaults[name]
        else:
            default = columns[name].default.arg if columns[name].default is not None else None
        if getattr(obj, name) != default:
            return True
    return False


def note_withdrawn(session: Session, mark_id: int, arrived: float) -> None:
    """What had arrived of a mark when it was taken back, for its log line (Spec 10.7, p280)."""
    if not session.info.get(_QUIET):
        _data(session).arrived[mark_id] = arrived


def _collect_row(session: Session, data: _Data, obj: object, change: str, actor: str) -> None:
    if isinstance(obj, Setting):
        _collect_setting(data, obj, change, actor)
    elif isinstance(obj, User):
        if change == "new":
            data.account_created = actor
        elif change == "dirty" and (_history(obj, "username")[0] or _history(obj, "password_hash")[0]):
            data.credentials_changed = actor
    elif isinstance(obj, (Article, Part, ProductSet)):
        kind = {Article: "article", Part: "part", ProductSet: "set"}[type(obj)]
        columns = {"article": ARTICLE_COLUMNS, "part": PART_COLUMNS, "set": SET_COLUMNS}[kind]
        thing = _thing(data, kind, obj.id, actor)
        if change == "new":
            thing.created = True
            return
        if change != "dirty":
            return
        for name in columns:
            if _history(obj, name)[0]:
                thing.fields.add(values.FIELDS[name])
        changed, before = _history(obj, "archived_at")
        if changed:
            thing.archived = obj.archived_at is not None
        if isinstance(obj, Article):
            changed, _ = _history(obj, "auto_enabled")
            if changed:
                thing.auto = bool(obj.auto_enabled)
    elif isinstance(obj, (ArticlePart, ArticleFilamentSort)):
        name = "parts" if isinstance(obj, ArticlePart) else "filaments"
        row = (
            (obj.part_id, obj.qty_per_unit)
            if isinstance(obj, ArticlePart)
            else (obj.slot, obj.sort_id, bool(obj.reordered))
        )
        thing = _thing(data, "article", obj.article_id, actor)
        if change == "new":
            thing.new_rows.setdefault(name, Counter())[row] += 1
        elif change == "deleted":
            thing.old_rows.setdefault(name, Counter())[row] += 1
        else:
            thing.fields.add(name)
    elif isinstance(obj, PartSupplier):
        # The suppliers are one field of the part (Spec 5.3, 10.7); saving compares by position,
        # so only a real change writes a row.
        thing = _thing(data, "part", obj.part_id, actor)
        row = (obj.position, obj.name, obj.order_url, obj.pack_size, obj.min_order_packs,
               obj.lead_time_days, bool(obj.is_default))
        if change == "new":
            thing.new_rows.setdefault("suppliers", Counter())[row] += 1
        elif change == "deleted":
            thing.old_rows.setdefault("suppliers", Counter())[row] += 1
        else:
            thing.fields.add("suppliers")
    elif isinstance(obj, SetPosition):
        # Base parts and choice fields are two fields of the set (Spec 5.4, 10.7; p340).
        data.positions[obj.id] = obj.set_id
        thing = _thing(data, "set", obj.set_id, actor)
        name = "base" if obj.slot == 0 else "choices"
        row = ("position", obj.slot, obj.sort, obj.qty)
        if change == "new":
            thing.new_rows.setdefault(name, Counter())[row] += 1
        elif change == "deleted":
            thing.old_rows.setdefault(name, Counter())[row] += 1
        else:
            thing.fields.add(name)
    elif isinstance(obj, SetPositionOption):
        set_id = _set_of_position(session, data, obj.position_id)
        if set_id is None:
            return
        thing = _thing(data, "set", set_id, actor)
        position = session.get(SetPosition, obj.position_id)
        slot = position.slot if position is not None else None
        name = "base" if slot == 0 else "choices"
        row = ("option", slot, position.sort if position is not None else None, obj.article_id, obj.suffix, obj.sort)
        if change == "new":
            thing.new_rows.setdefault(name, Counter())[row] += 1
        elif change == "deleted":
            thing.old_rows.setdefault(name, Counter())[row] += 1
        else:
            thing.fields.add(name)
    elif isinstance(obj, FilamentSettings):
        if (change == "new" and _differs_from_defaults(obj)) or (
            change == "dirty" and any(_history(obj, name)[0] for name in FILAMENT_COLUMNS)
        ):
            data.filament.setdefault(obj.spool_filament_id, actor)
    elif isinstance(obj, FilamentSupplier):
        # The suppliers are part of the order settings of the sort (Spec 9.3, 10.7); saving
        # compares by position, so only a real change writes a row.
        if (
            (change == "new" and _differs_from_defaults(obj))
            or change == "deleted"
            or (change == "dirty" and any(_history(obj, name)[0] for name in SUPPLIER_COLUMNS))
        ):
            data.filament.setdefault(obj.sort_id, actor)
    elif isinstance(obj, ReorderMark):
        if change == "new":
            data.ordered.append((obj.id, actor))
        elif change == "dirty":
            changed, before = _history(obj, "received_at")
            if changed and before is None and obj.received_at is not None:
                (data.withdrawn if obj.withdrawn else data.received).append((obj.id, actor))


def _after_flush(session: Session, _flush_context: object) -> None:
    if session.info.get(_QUIET):
        return
    try:
        data = _data(session)
        actor = _actor(session)
        if session.info.get(VIA) is not None:
            data.meta["via"] = session.info.get(VIA)
        if session.info.get(ADDRESS) is not None:
            data.meta["address"] = session.info.get(ADDRESS)
        rows = [(obj, "new") for obj in session.new]
        rows += [(obj, "dirty") for obj in session.dirty if session.is_modified(obj)]
        rows += [(obj, "deleted") for obj in session.deleted]
        # Positions first: an option looks up its set through them.
        rows.sort(key=lambda item: 0 if isinstance(item[0], SetPosition) else 1)
        for obj, change in rows:
            _collect_row(session, data, obj, change, actor)
    except Exception as error:  # the change is saved anyway; only its line is missing
        # The kind of error only: a traceback could carry the values of a statement (Spec 11.5a).
        log.error("activity log: a change could not be collected (%s)", type(error).__name__)
        session.info[_DATA] = _Data()


# ---- building the lines ----------------------------------------------------------------------


def _entry(data: _Data, kind: str, actor: str, details: dict[str, Any] | None, *, address: bool = False) -> ActivityEntry:
    return ActivityEntry(
        area=area_of(kind),
        kind=kind,
        actor=actor,
        via=data.meta.get("via"),
        address=data.meta.get("address") if address else None,
        details=details,
    )


def _settings_entries(session: Session, data: _Data) -> list[ActivityEntry]:
    by_actor: dict[str, list[dict[str, object]]] = {}
    owner: dict[str, object] = {}
    for key in values.SETTINGS:  # the order of the settings pages, not the order of saving
        if key not in data.settings:
            continue
        old, new, actor = data.settings[key]
        if old == new:
            continue
        by_actor.setdefault(actor, []).append(values.shown_change(key, old, new))
        if key.startswith("bambuddy_"):
            owner[actor] = st.get_setting(session, "bambuddy_username")  # as stored now, changed or not
    entries = []
    for actor, changes in by_actor.items():
        details: dict[str, object] = {"settings": changes}
        if actor in owner:
            details["owner"] = owner[actor]
        entries.append(_entry(data, "settings_changed", actor, details))
    return entries


def _rows_changed(thing: _Thing, name: str) -> bool:
    return thing.old_rows.get(name, Counter()) != thing.new_rows.get(name, Counter())


def _name(session: Session, kind: str, ident: int) -> str | None:
    model = {"article": Article, "part": Part, "set": ProductSet}[kind]
    obj = session.get(model, ident)
    return obj.name if obj is not None else None


def _thing_entries(session: Session, data: _Data) -> list[ActivityEntry]:
    entries = []
    for (kind, ident), thing in data.things.items():
        name = _name(session, kind, ident)
        if name is None:
            continue  # created and deleted again in one transaction
        ref = {"id": ident, "name": name}
        if thing.created:
            entries.append(_entry(data, f"{kind}_created", thing.actor, ref))
            continue
        fields = set(thing.fields)
        for rows in set(thing.old_rows) | set(thing.new_rows):
            if _rows_changed(thing, rows):
                fields.add(rows)
        if thing.archived is not None:
            entries.append(_entry(data, f"{kind}_{'archived' if thing.archived else 'restored'}", thing.actor, ref))
        if thing.auto is not None:
            entries.append(_entry(data, "article_auto", thing.actor, {**ref, "on": thing.auto}))
        if not fields:
            continue
        if kind == "article" and fields == {"min_stock"}:
            entries.append(_entry(data, "article_target", thing.actor, ref))
            continue
        ordered = [name for name in dict.fromkeys(values.FIELDS.values()) if name in fields]
        entries.append(_entry(data, f"{kind}_changed", thing.actor, {**ref, "fields": ordered}))
    return entries


def _mark_name(session: Session, mark_id: int) -> tuple[str, int, str | None] | None:
    mark = session.get(ReorderMark, mark_id)
    if mark is None:
        return None
    if mark.item_type == "part":
        part = session.get(Part, mark.item_id)
        return "part", mark.item_id, part.name if part is not None else None
    sort = session.get(FilamentSort, mark.item_id)
    return mark.item_type, mark.item_id, sort.label if sort is not None else None


def _build(session: Session, data: _Data) -> list[ActivityEntry]:
    entries: list[ActivityEntry] = []
    if data.account_created is not None:
        # The setup saves the language with the account: one line, "account created".
        entries.append(_entry(data, "account_created", data.account_created, None, address=True))
    else:
        entries += _settings_entries(session, data)
    if data.credentials_changed is not None:
        entries.append(_entry(data, "credentials_changed", data.credentials_changed, None, address=True))
    entries += _thing_entries(session, data)
    for sort_id, actor in data.filament.items():
        sort = session.get(FilamentSort, sort_id)
        entries.append(
            _entry(data, "filament_settings_changed", actor, {"id": sort_id, "name": sort.label if sort else None})
        )
    for kind, marks in (("reorder_ordered", data.ordered), ("reorder_received", data.received)):
        for mark_id, actor in marks:
            item = _mark_name(session, mark_id)
            if item is not None:
                item_type, item_id, name = item
                entries.append(_entry(data, kind, actor, {"type": item_type, "id": item_id, "name": name}))
    for mark_id, actor in data.withdrawn:
        entries.append(_entry(data, "reorder_withdrawn", actor, _withdrawn_details(session, data, mark_id)))
    return entries


def _withdrawn_details(session: Session, data: _Data, mark_id: int) -> dict[str, Any]:
    """Spec 10.7 (p280): the ordered amount, the supplier and what had arrived; ids and numbers only,
    the page writes the words. The unit is the part's ("pcs") or "g" for filament."""
    mark = session.get(ReorderMark, mark_id)
    item_type, item_id, name = _mark_name(session, mark_id) or (mark.item_type, mark.item_id, None)
    part = session.get(Part, item_id) if item_type == "part" else None
    arrived = data.arrived.get(mark_id, 0)
    return {
        "type": item_type, "id": item_id, "name": name, "packs": mark.qty, "size": mark.pack_size,
        "unit": part.unit if part is not None else "g", "supplier": mark.supplier_name,
        "supplier_position": mark.supplier_position, "arrived": int(arrived) if arrived == int(arrived) else arrived,
    }


def _before_commit(session: Session) -> None:
    if session.in_nested_transaction():
        return
    # Outside the guard below: a failing save of the change itself keeps its own error and is not
    # taken for the log's (review v0.3.13 C, finding 1).
    session.flush()  # what is still pending is collected, too
    data = session.info.pop(_DATA, None)
    if data is None:
        return
    try:
        entries = _build(session, data)
        for entry in entries:
            json.dumps(entry.details)  # what the JSON column could not store fails here, not in the commit
    except Exception as error:  # the change is committed anyway; only its line is missing (Spec 10.7)
        # The kind of error only: a traceback could carry the values of a statement (Spec 11.5a).
        log.error("activity log: a line could not be built (%s)", type(error).__name__)
        return
    if entries and _has_log_table(session):
        session.add_all(entries)


# Engines whose database has the table of the log (checked once each; a migration adds it, never removes it).
_WITH_TABLE: "weakref.WeakSet[object]" = weakref.WeakSet()  # weak: an id could be reused by another engine


def _has_log_table(session: Session) -> bool:
    """False for a database of an older version written before its migration: its change counts, the
    line is left out (Spec 10.7 "die Änderung selbst gilt trotzdem")."""
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine in _WITH_TABLE:
        return True
    if inspect(session.connection()).has_table(ActivityEntry.__tablename__):
        _WITH_TABLE.add(engine)
        return True
    log.warning("activity log: this database has no table for it yet; the line is left out")
    return False


def _after_commit(session: Session) -> None:
    if not session.in_nested_transaction():
        session.info.pop(_DATA, None)  # the lines' own flush in the commit collected nothing


def _after_rollback(session: Session) -> None:
    if session.in_nested_transaction():
        return  # a savepoint: what the outer transaction collected stays
    session.info.pop(_DATA, None)


def install_capture(maker: sessionmaker) -> None:
    """Log the changes of every session of `maker` (Spec 10.7 "nicht vergessbar")."""
    event.listen(maker, "after_flush", _after_flush)
    event.listen(maker, "before_commit", _before_commit)
    event.listen(maker, "after_commit", _after_commit)
    event.listen(maker, "after_rollback", _after_rollback)
