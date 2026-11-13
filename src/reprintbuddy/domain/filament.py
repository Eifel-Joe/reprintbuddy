"""Spool sorts, the filament group of each slot, their stock and the daily record (Spec 4, 5.2, 9.1, 9.2).

A spool sort is brand + material + subtype + colour value of Bambuddy's spools, nothing merged. Which
spool sorts an article may use is ticked per slot (the filament group); one of them is reordered.
Functions never commit; the caller owns the transaction.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.activity.capture import quiet
from reprintbuddy.bambuddy.client import BambuddyClient
from reprintbuddy.bambuddy.types import Spool
from reprintbuddy.domain import color, inputs, rules, suppliers
from reprintbuddy.domain import articlesets
from reprintbuddy.domain import catalog
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticleFilamentSort,
    FilamentSettings,
    FilamentSnapshot,
    FilamentSort,
    FilamentSupplier,
    JournalEntry,
    ReorderMark,
    utcnow,
)

WINDOW_DAYS = 30


@dataclass(frozen=True)
class SortStock:
    sort_id: int
    label: str
    material: str
    color_hex: str | None
    stock_g: float
    spools: int


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def _text(value: str | None) -> str:
    return (value or "").strip()


def sort_key(brand: str | None, material: str | None, subtype: str | None, color_hex: str | None) -> str:
    """Identity of a spool sort: brand, material, subtype and colour value, written alike (Spec 9.1).
    The colour name does not count."""
    return "|".join(_norm(x) for x in (brand, material, subtype, color_hex))


def sort_label(brand: str | None, material: str | None, subtype: str | None, color_name: str | None,
               color_hex: str | None) -> str:
    """Non-empty parts of brand, material, subtype and colour name; the colour value (`#RRGGBB` or
    "transparent") stands in for a missing name."""
    name = _text(color_name)
    if not name:
        last = color.display_hex(color_hex)
    elif _norm(name) == _norm(subtype):
        last = ""  # the name repeats the subtype: shown once, and no colour in its place
    else:
        last = name
    return " ".join(_text(p) for p in (brand, material, subtype, last) if _text(p))


def _spool_key(spool: Spool) -> str:
    return sort_key(spool.brand, spool.material, spool.subtype, spool.color_hex)


def _current_sorts(session: Session) -> dict[str, FilamentSort]:
    return {
        s.sort_key: s
        for s in session.scalars(select(FilamentSort).where(FilamentSort.legacy.is_(False)))
    }


def resolve_sorts(session: Session, spools: Sequence[Spool]) -> dict[int, int | None]:
    """Spool id -> id of its stored spool sort (None if there is none yet); writes nothing."""
    known = _current_sorts(session)
    return {s.id: (known[k].id if (k := _spool_key(s)) in known else None) for s in spools}


def sync_sorts(session: Session, spools: Sequence[Spool], archived: Sequence[Spool] = ()) -> dict[int, int]:
    """Assign every spool to its spool sort and create the missing ones; returns spool id -> sort id.

    The name of a sort comes from its lowest-id spool that has one; a spool without a name never
    erases a known one. The daily rows of every listed spool follow it to its spool sort (the history
    belongs to the spool), and the sorts of 0006 are converted once (`convert_legacy`; the
    `archived` spools of the list count there for the majority, they get no sort here).
    """
    known = _current_sorts(session)
    ordered = sorted(spools, key=lambda s: s.id)
    assigned: dict[int, int] = {}
    named: dict[int, Spool] = {}
    for spool in ordered:
        key = _spool_key(spool)
        sort = known.get(key)
        if sort is None:
            sort = FilamentSort(
                sort_key=key,
                brand=_text(spool.brand) or None,
                material=_text(spool.material),
                subtype=_text(spool.subtype) or None,
                color_hex=spool.color_hex,
                color_name=None,
                label=sort_label(spool.brand, spool.material, spool.subtype, None, spool.color_hex),
            )
            session.add(sort)
            session.flush()
            known[key] = sort
        assigned[spool.id] = sort.id
        if _text(spool.color_name) and sort.id not in named:
            named[sort.id] = spool
    by_id = {s.id: s for s in known.values()}
    for sort_id, spool in named.items():
        sort = by_id[sort_id]
        if sort.color_name != spool.color_name:
            sort.color_name = spool.color_name
            sort.label = sort_label(sort.brand, sort.material, sort.subtype, spool.color_name, sort.color_hex)
    _follow_spools(session, assigned)
    convert_legacy(session, ordered, assigned, archived)
    session.flush()
    return assigned


def _follow_spools(session: Session, assigned: Mapping[int, int]) -> None:
    """The daily rows of a spool belong to the spool sort it is in now (also those of earlier days)."""
    by_sort: dict[int, list[int]] = defaultdict(list)
    for spool_id, sort_id in assigned.items():
        by_sort[sort_id].append(spool_id)
    # Only the rows that differ, and row by row: a bulk UPDATE would report a change on every page
    # view, and loading every day of every spool would grow with the record (review v0.3.7 A, 4).
    for sort_id, spool_ids in by_sort.items():
        rows = session.scalars(
            select(FilamentSnapshot).where(
                FilamentSnapshot.spool_id.in_(spool_ids), FilamentSnapshot.spool_filament_id != sort_id
            )
        )
        for row in rows:
            row.spool_filament_id = sort_id


# ---- conversion of the brandless sorts of 0006 (migration 0007) -------------------------------

_LEGACY_SIMILAR = 7.0  # frozen: the threshold the sorts of 0006 were built with


def _legacy_nearest(sorts: Iterable[FilamentSort], color_hex: str | None) -> FilamentSort | None:
    """The rule of 0006 (v0.3.5): the nearest anchor of one material and subtype within delta E 7,
    a tie to the lower id. Kept only to find the spools of a legacy sort."""
    best: tuple[float, int] | None = None
    found: FilamentSort | None = None
    for sort in sorts:
        distance = color.distance(sort.color_hex, color_hex)
        if distance is None or distance > _LEGACY_SIMILAR:
            continue
        if best is None or (distance, sort.id) < best:
            best, found = (distance, sort.id), sort
    return found


_SETTINGS = ("safety_value", "safety_unit")  # the rest sits at the suppliers (0016)


def keep_roll_size(session: Session, mark: ReorderMark) -> int:
    """The roll size a filament mark counts in, kept at the mark (Spec 9.4, p281); a mark that kept
    none (written before 0016 filled them) takes its sort's default supplier, without settings the
    default unit of 1000 g, and keeps it from now on."""
    if mark.pack_size is None:
        default = default_supplier(session, mark.item_id)
        mark.pack_size = default.pack_size if default is not None else DEFAULT_ROLL_GRAMS
    return mark.pack_size


def convert_legacy(
    session: Session, spools: Sequence[Spool], assigned: Mapping[int, int], archived: Sequence[Spool] = ()
) -> int:
    """Move the settings and open "ordered" marks of the sorts of 0006 to spool sorts; returns how
    many legacy sorts had something to move. Runs with every spool list; once done, nothing is left.

    The spools of a legacy sort (living and archived) are found by the rule it was built with
    (`_legacy_nearest`). The target is the spool sort (by key) with the most of them, a tie to the
    lower id. If that spool sort is not stored yet (all its spools archived) or no spool belongs to the
    legacy sort, it waits: nothing is dropped or closed, the mark stays "on its way" and a later list
    converts (Hauptsitzung 07.11.2026, against ordering twice). Settings move only to a target
    without settings, with their suppliers, else they are dropped; open marks move to the target with
    the roll size they were ordered in (Spec 9.4), two open marks there become one. Each converted sort gets a journal row. Legacy sorts stay as
    hidden rows, so that old received marks and the daily rows of gone spools keep a sort to point at.
    """
    # The journal has it ("filament_sorts_converted"); the moved settings and marks are not the
    # user's changes and stay out of the activity log (Spec 10.7).
    with quiet(session):
        return _convert_legacy(session, spools, assigned, archived)


def _convert_legacy(
    session: Session, spools: Sequence[Spool], assigned: Mapping[int, int], archived: Sequence[Spool] = ()
) -> int:
    pending = [
        sort
        for sort in session.scalars(
            select(FilamentSort).where(FilamentSort.legacy.is_(True)).order_by(FilamentSort.id)
        )
        if session.get(FilamentSettings, sort.id) is not None or _open_filament_marks(session, sort.id)
    ]
    if not pending or not (spools or archived):
        # An empty list (a fresh Bambuddy, another spool mode) is no evidence that the spools are gone:
        # nothing is dropped or closed, the next list with spools converts (review v0.3.7 A, 1).
        return 0
    all_legacy = session.scalars(
        select(FilamentSort).where(FilamentSort.legacy.is_(True)).order_by(FilamentSort.id)
    ).all()
    groups: dict[tuple[str, str], list[FilamentSort]] = defaultdict(list)
    for sort in all_legacy:
        groups[(_norm(sort.material), _norm(sort.subtype))].append(sort)
    stored = _current_sorts(session)
    members: dict[int, list[str]] = defaultdict(list)
    for spool in [*spools, *archived]:
        home = _legacy_nearest(groups.get((_norm(spool.material), _norm(spool.subtype)), ()), spool.color_hex)
        if home is not None:
            members[home.id].append(_spool_key(spool))
    now = utcnow()
    converted = 0
    for old in pending:
        counts = Counter(members.get(old.id, ()))
        if not counts:
            continue  # no spool of it in the list: it waits
        # most spools first; a tie to the stored sort with the lower id, unknown sorts last
        key = min(counts, key=lambda k: (-counts[k], stored[k].id if k in stored else float("inf"), k))
        target = stored.get(key)
        if target is None:
            continue  # the winning spool sort has only archived spools so far: it waits
        _convert_one(session, old, target, now)
        converted += 1
    session.flush()
    return converted


def _open_filament_marks(session: Session, sort_id: int) -> list[ReorderMark]:
    return list(
        session.scalars(
            select(ReorderMark)
            .where(
                ReorderMark.item_type == "filament",
                ReorderMark.item_id == sort_id,
                ReorderMark.received_at.is_(None),
            )
            .order_by(ReorderMark.marked_at, ReorderMark.id)
        )
    )


def _convert_one(session: Session, old: FilamentSort, target: FilamentSort, now: datetime) -> None:
    details: dict[str, object] = {"from": old.id, "from_label": old.label, "into": target.id, "into_label": target.label}
    notes = []
    # A mark keeps the roll size it was ordered in (Spec 9.4, p281); one from before that kept none
    # counts in its sort's roll size of now. Fixed before the settings move.
    for mark in _open_filament_marks(session, old.id):
        keep_roll_size(session, mark)
    settings = session.get(FilamentSettings, old.id)
    if settings is not None:
        values: dict[str, object] = {f: getattr(settings, f) for f in _SETTINGS}
        rows = sort_suppliers(session, old.id)
        if session.get(FilamentSettings, target.id) is None:
            # the suppliers follow their settings; the target's row comes first for the foreign key
            session.add(FilamentSettings(spool_filament_id=target.id, **values))
            session.flush()
            for row in rows:
                row.sort_id = target.id
            details["settings"] = "moved"
            notes.append("settings moved")
        else:
            values["suppliers"] = [{c: getattr(row, c) for c in suppliers.COLUMNS} for row in rows]
            for row in rows:
                session.delete(row)
            details["settings"] = "dropped"
            details["dropped_settings"] = values
            notes.append("settings dropped")
        session.flush()
        session.delete(settings)
        session.flush()
    marks = _open_filament_marks(session, old.id)
    if marks:
        for mark in marks:
            mark.item_id = target.id
        session.flush()
        joined = _open_filament_marks(session, target.id)
        if len(joined) > 1:
            # one open mark per sort (Spec 9.4): the earliest takes the grams of all, in its rolls
            first = joined[0]
            grams = sum(m.qty * keep_roll_size(session, m) for m in joined)
            first.qty = max(1, math.ceil(grams / first.pack_size))
            for mark in joined[1:]:
                session.delete(mark)
        details["marks"] = "moved"
        notes.append("ordered mark moved")
    session.add(
        JournalEntry(
            actor="auto",
            kind="filament_sorts_converted",
            qty=0,
            note=f"{old.label} -> {target.label}: " + ", ".join(notes),
            details=details,
        )
    )
    session.flush()


# ---- stock -------------------------------------------------------------------------------------


def stock_by_sort(session: Session, spools: Sequence[Spool]) -> dict[int, SortStock]:
    """Live stock per spool sort: the summed `remaining_g` of the non-archived spools (Spec 9.1)."""
    live = [s for s in spools if not s.archived]
    ids = sync_sorts(session, live, [s for s in spools if s.archived])
    grams: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    for spool in live:
        sort_id = ids[spool.id]
        grams[sort_id] += spool.remaining_g
        count[sort_id] += 1
    return {
        sort_id: _entry(session.get(FilamentSort, sort_id), grams[sort_id], count[sort_id])
        for sort_id in grams
    }


def _entry(sort: FilamentSort, grams: float, spools: int) -> SortStock:
    return SortStock(sort.id, sort.label, sort.material, sort.color_hex, grams, spools)


def stock_from_snapshots(session: Session) -> tuple[dict[int, SortStock], date | None]:
    """Stock from the rows of the latest recorded day; `({}, None)` without any record. Rows of a
    legacy sort (not yet converted) count for nothing."""
    latest = session.scalar(select(func.max(FilamentSnapshot.day)))
    if latest is None:
        return {}, None
    grams: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    rows = session.scalars(
        select(FilamentSnapshot)
        .join(FilamentSort, FilamentSort.id == FilamentSnapshot.spool_filament_id)
        .where(FilamentSnapshot.day == latest, FilamentSort.legacy.is_(False))
    )
    for row in rows:
        grams[row.spool_filament_id] += row.remaining_g
        count[row.spool_filament_id] += 1
    return {
        sort_id: _entry(session.get(FilamentSort, sort_id), grams[sort_id], count[sort_id])
        for sort_id in grams
    }, latest


def record_snapshot(session: Session, spools: Sequence[Spool], day: date) -> int:
    """One row per non-archived spool and day; a second call on the same day overwrites."""
    live = [s for s in spools if not s.archived]
    ids = sync_sorts(session, live, [s for s in spools if s.archived])
    for spool in live:
        row = session.scalar(
            select(FilamentSnapshot).where(
                FilamentSnapshot.day == day, FilamentSnapshot.spool_id == spool.id
            )
        )
        if row is None:
            row = FilamentSnapshot(day=day, spool_id=spool.id)
            session.add(row)
        row.spool_filament_id = ids[spool.id]
        row.remaining_g = spool.remaining_g
        row.label_g = spool.label_g
        row.spool_created_at = spool.created_at
    # a spool recorded earlier today that is archived or gone now must not count any more
    gone = select(FilamentSnapshot).where(FilamentSnapshot.day == day)
    if live:
        gone = gone.where(FilamentSnapshot.spool_id.not_in([s.id for s in live]))
    for row in session.scalars(gone).all():
        session.delete(row)
    session.flush()
    return len(live)


def consumption(session: Session, sort_ids: int | Collection[int], today: date) -> tuple[float, int]:
    """Grams per day and days of history over the last 30 days, of one spool sort or the sum of
    several (Spec 9.2: the sorts ticked in the slots of a reordered sort)."""
    ids = [sort_ids] if isinstance(sort_ids, int) else list(sort_ids)
    if not ids:
        return 0.0, 0
    rows = session.execute(
        select(FilamentSnapshot.spool_id, FilamentSnapshot.day, FilamentSnapshot.remaining_g)
        .where(
            FilamentSnapshot.spool_filament_id.in_(ids),
            FilamentSnapshot.day >= today - timedelta(days=WINDOW_DAYS),
            FilamentSnapshot.day <= today,
        )
        .order_by(FilamentSnapshot.day)
    )
    series: dict[int, list[tuple[date, float]]] = defaultdict(list)
    for spool_id, day, grams in rows:
        series[spool_id].append((day, grams))
    return rules.consumption_per_day(series)


def known_sorts(
    session: Session, spools: Sequence[Spool] | None
) -> tuple[dict[int, SortStock] | None, date | None]:
    """Stock of every known spool sort: live with `spools` (date None), else from the record.

    Sorts of the directory without stock show 0; legacy sorts are left out. `(None, None)`: nothing
    known (Anti-Ziel 4).
    """
    if spools is not None:
        stock, day = stock_by_sort(session, spools), None
    else:
        stock, day = stock_from_snapshots(session)
        if day is None:
            return None, None
    for sort in session.scalars(select(FilamentSort).where(FilamentSort.legacy.is_(False))):
        if sort.id not in stock:
            stock[sort.id] = _entry(sort, 0.0, 0)
    return stock, day


# ---- the filament group of a slot (Spec 5.2, 9.1) --------------------------------------------


def is_candidate(sort: FilamentSort, material: str | None, slot_hex: str | None) -> bool:
    """A spool sort fits a slot: the same material and a similar colour (delta E 7; alpha 00 only to
    alpha 00). Subtype and brand do not count. A slot without material or colour fits nothing."""
    if sort.legacy or not _norm(material) or not _norm(slot_hex):
        return False
    return _norm(sort.material) == _norm(material) and color.similar(sort.color_hex, slot_hex)


@dataclass(frozen=True)
class SlotGroup:
    slot: int
    grams: float
    material: str | None
    color_hex: str | None
    choices: tuple[int, ...]  # the candidates, plus ticked sorts that no longer fit; by label
    ticked: frozenset[int]
    reordered: int | None


def _ticked_rows(session: Session, article_id: int) -> dict[int, list[ArticleFilamentSort]]:
    rows: dict[int, list[ArticleFilamentSort]] = defaultdict(list)
    for row in session.scalars(select(ArticleFilamentSort).where(ArticleFilamentSort.article_id == article_id)):
        rows[row.slot].append(row)
    return rows


@dataclass(frozen=True)
class Suggestion:
    """What the filament page of a new article may tick for one slot, taken from a similar article."""

    source_id: int
    source_name: str
    sort_ids: frozenset[int]
    reordered: int | None


def suggest_from_similar(session: Session, article_id: int) -> dict[int, Suggestion]:
    """For each slot of a new article the group of a similar article (Spec 5.2, 9.1, p156); nothing is stored.

    Similar: any slot of another article that is not archived with the same material and colour
    (`catalog.same_filament_kind`, the rule of keeping a group after a change of the print file); the slot
    NUMBER does not matter, the same colour may sit in another slot of another file. The source must have
    a group in such a slot (an empty slot has nothing to suggest). Of several articles the one created
    last counts (highest id: there is no date of an assignment; the owner's decision); of several such slots of
    that article the one with the smallest number that has a group. Only sorts the new slot offers itself
    (`is_candidate`) are suggested; the reordered one is the source's if it is still there, else the only
    sort left, else none (saving asks the person to choose). A slot without material or colour gets none."""
    slots = session.scalars(
        select(ArticleFilament).where(ArticleFilament.article_id == article_id).order_by(ArticleFilament.slot)
    ).all()
    wanted = [slot for slot in slots if _norm(slot.material) and _norm(slot.color_hex)]
    if not wanted:
        return {}
    sources = session.execute(
        select(Article.id, Article.name)
        .where(Article.id != article_id, articlesets.where_print())
        .order_by(Article.id.desc())
    ).all()
    kinds: dict[int, list[tuple[int, str | None, str | None]]] = defaultdict(list)
    for row in session.scalars(
        select(ArticleFilament).where(ArticleFilament.article_id.in_([s.id for s in sources])).order_by(ArticleFilament.slot)
    ):
        kinds[row.article_id].append((row.slot, row.material, row.color_hex))
    ticked: dict[int, dict[int, list[ArticleFilamentSort]]] = defaultdict(lambda: defaultdict(list))
    for row in session.scalars(
        select(ArticleFilamentSort).where(ArticleFilamentSort.article_id.in_([s.id for s in sources]))
    ):
        ticked[row.article_id][row.slot].append(row)
    sorts = {s.id: s for s in session.scalars(select(FilamentSort).where(FilamentSort.legacy.is_(False)))}
    found: dict[int, Suggestion] = {}
    for slot in wanted:
        for source in sources:
            for number, material, color_hex in kinds.get(source.id, ()):
                if not catalog.same_filament_kind((material, color_hex), (slot.material, slot.color_hex)):
                    continue
                rows = [
                    r for r in ticked[source.id].get(number, ())
                    if r.sort_id in sorts and is_candidate(sorts[r.sort_id], slot.material, slot.color_hex)
                ]
                if not rows:
                    continue
                ids = frozenset(r.sort_id for r in rows)
                named = next((r.sort_id for r in rows if r.reordered), None)
                reordered = named if named is not None else (next(iter(ids)) if len(ids) == 1 else None)
                found[slot.slot] = Suggestion(source.id, source.name, ids, reordered)
                break
            if slot.slot in found:
                break
    return found


def slot_groups(session: Session, article_id: int) -> list[SlotGroup]:
    """The filament slots of an article by slot number, with their candidates and what is ticked."""
    slots = session.scalars(
        select(ArticleFilament).where(ArticleFilament.article_id == article_id).order_by(ArticleFilament.slot)
    ).all()
    sorts = session.scalars(select(FilamentSort).where(FilamentSort.legacy.is_(False))).all()
    by_id = {s.id: s for s in sorts}
    ticked_rows = _ticked_rows(session, article_id)
    result = []
    for slot in slots:
        ticked = {r.sort_id for r in ticked_rows.get(slot.slot, ())}
        reordered = next((r.sort_id for r in ticked_rows.get(slot.slot, ()) if r.reordered), None)
        ids = {s.id for s in sorts if is_candidate(s, slot.material, slot.color_hex)} | ticked
        choices = tuple(sorted(ids, key=lambda i: (by_id[i].label.casefold(), i) if i in by_id else ("", i)))
        result.append(
            SlotGroup(slot.slot, slot.grams, slot.material, slot.color_hex, choices, frozenset(ticked), reordered)
        )
    return result


def set_groups(
    session: Session, article_id: int, groups: Mapping[int, tuple[Collection[int], int | None]]
) -> None:
    """Store `slot -> (ticked sort ids, reordered sort id)`; slots not named stay. Everything is
    checked before anything is written. Only candidates of the slot (or sorts already ticked there)
    can be ticked; one ticked sort is reordered by itself, of several the reordered one must be named."""
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    slots = {
        r.slot: r
        for r in session.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    }
    before = _ticked_rows(session, article_id)
    plan: dict[int, tuple[set[int], int | None]] = {}
    for slot_no, (ticked_ids, reordered) in groups.items():
        slot = slots.get(slot_no)
        if slot is None:
            raise ValidationError("filament.unknown_slot")
        ticked = set(ticked_ids)
        already = {r.sort_id for r in before.get(slot_no, ())}
        for sort_id in ticked:
            sort = session.get(FilamentSort, sort_id)
            if sort is None or sort.legacy:
                raise NotFoundError("filament.sort_not_found")
            if sort_id not in already and not is_candidate(sort, slot.material, slot.color_hex):
                raise ValidationError("filament.not_a_candidate")
        if not ticked:
            reordered = None
        elif len(ticked) == 1:
            reordered = next(iter(ticked))
        elif reordered is None:
            raise ValidationError("filament.choose_reordered")
        elif reordered not in ticked:
            raise ValidationError("filament.reordered_not_ticked")
        plan[slot_no] = (ticked, reordered)
    for slot_no, (ticked, reordered) in plan.items():
        for row in before.get(slot_no, ()):
            session.delete(row)  # row by row, so the change feed names the article (changes.py)
        session.flush()
        for sort_id in sorted(ticked):
            session.add(
                ArticleFilamentSort(
                    article_id=article_id, slot=slot_no, sort_id=sort_id, reordered=sort_id == reordered
                )
            )
    session.flush()


@dataclass(frozen=True)
class ReorderGroup:
    """A spool sort that is reordered in at least one slot of an active article (Spec 9.2)."""

    sort_id: int
    slots: tuple[tuple[int, int], ...]  # (article id, slot) where it is reordered
    members: frozenset[int]  # every spool sort ticked in these slots, itself included


def reorder_groups(session: Session) -> dict[int, ReorderGroup]:
    rows = session.execute(
        select(ArticleFilamentSort)
        .join(Article, Article.id == ArticleFilamentSort.article_id)
        .where(articlesets.where_print())
    ).scalars()
    by_slot: dict[tuple[int, int], list[ArticleFilamentSort]] = defaultdict(list)
    for row in rows:
        by_slot[(row.article_id, row.slot)].append(row)
    slots: dict[int, list[tuple[int, int]]] = defaultdict(list)
    members: dict[int, set[int]] = defaultdict(set)
    for key, ticked in sorted(by_slot.items()):
        reordered = next((r.sort_id for r in ticked if r.reordered), None)
        if reordered is None:
            continue
        slots[reordered].append(key)
        members[reordered].update(r.sort_id for r in ticked)
    return {sid: ReorderGroup(sid, tuple(slots[sid]), frozenset(members[sid])) for sid in slots}


def ticked_sort_ids(session: Session) -> set[int]:
    """Ids of the spool sorts ticked in a slot of a non-archived article (what the filament page lists)."""
    rows = session.scalars(
        select(ArticleFilamentSort.sort_id)
        .join(Article, Article.id == ArticleFilamentSort.article_id)
        .where(articlesets.where_print())
        .distinct()
    )
    return set(rows)


def unmapped_slots(session: Session, article_id: int | None = None) -> list[tuple[int, str, int]]:
    """(article id, name, slot) of every slot without a ticked spool sort, active articles only
    ("Filament nicht zugeordnet", Spec 9.1); with `article_id` only that article's slots."""
    ticked = (
        select(ArticleFilamentSort.slot)
        .where(
            ArticleFilamentSort.article_id == ArticleFilament.article_id,
            ArticleFilamentSort.slot == ArticleFilament.slot,
        )
        .exists()
    )
    stmt = (
        select(Article.id, Article.name, ArticleFilament.slot)
        .join(ArticleFilament, ArticleFilament.article_id == Article.id)
        .where(articlesets.where_print(), ~ticked)
        .order_by(Article.name, ArticleFilament.slot)
    )
    if article_id is not None:
        stmt = stmt.where(Article.id == article_id)
    return [(a, n, s) for a, n, s in session.execute(stmt)]


# ---- settings of a spool sort (Spec 9.3) ------------------------------------------------------


DEFAULT_ROLL_GRAMS = 1000  # the roll size a new supplier starts with; a mark without one counts in it


def _one_default() -> list[suppliers.SupplierInput]:
    return [suppliers.SupplierInput(pack_size=DEFAULT_ROLL_GRAMS, is_default=True)]


@dataclass
class FilamentSettingsInput:
    """The order settings of a spool sort (Spec 9.3): the safety margin, and 1 to 10 suppliers in
    the order of the form, exactly one the default. They only serve the reorder suggestion;
    filament is never booked in Reprintbuddy (Spec 9.1)."""

    safety_value: int = 0
    safety_unit: Literal["days", "grams"] = "days"
    suppliers: list[suppliers.SupplierInput] = field(default_factory=_one_default)


def get_settings(session: Session, sort_id: int) -> FilamentSettings | None:
    return session.get(FilamentSettings, sort_id)


def sort_suppliers(session: Session, sort_id: int) -> list[FilamentSupplier]:
    """The suppliers of a spool sort in the order of its form; none without settings."""
    return list(
        session.scalars(
            select(FilamentSupplier).where(FilamentSupplier.sort_id == sort_id).order_by(FilamentSupplier.position)
        )
    )


def default_supplier(session: Session, sort_id: int) -> FilamentSupplier | None:
    return session.scalar(
        select(FilamentSupplier).where(FilamentSupplier.sort_id == sort_id, FilamentSupplier.is_default.is_(True))
    )


def save_settings(session: Session, sort_id: int, data: FilamentSettingsInput) -> FilamentSettings:
    """Create or replace the settings of a spool sort; everything is checked before anything is stored."""
    sort = session.get(FilamentSort, sort_id)
    if sort is None or sort.legacy:
        raise NotFoundError("filament.sort_not_found")
    if data.safety_unit not in ("days", "grams"):
        raise ValidationError("filament.invalid_safety_unit")
    safety = inputs.int_in(data.safety_value, "filament.field.safety_value", 0, 1_000_000)
    wanted = suppliers.check(data.suppliers, suppliers.FILAMENT_LABELS)
    row = session.get(FilamentSettings, sort_id)
    if row is None:
        row = FilamentSettings(spool_filament_id=sort_id)
        session.add(row)
    if row.safety_value != safety:
        row.safety_value = safety
    if row.safety_unit != data.safety_unit:
        row.safety_unit = data.safety_unit
    session.flush()
    suppliers.apply(
        session, sort_suppliers(session, sort_id), wanted,
        lambda position: FilamentSupplier(sort_id=sort_id, position=position),
    )
    return row


# ---- hook in the regular tick (Entscheidung 16) ----------------------------------------------


async def poll_hook(client: BambuddyClient, sm: sessionmaker, now: datetime) -> None:
    """Record the day's spool stock and close arrived marks; errors go to the engine."""
    from reprintbuddy.domain import reorder  # reorder imports this module

    spools = await client.list_spools()  # no transaction open during the network call
    with sm.begin() as session:
        record_snapshot(session, spools, now.date())
        reorder.close_arrived_marks(session, spools, now)
