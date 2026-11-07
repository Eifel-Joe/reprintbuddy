"""Filament sorts, their stock and the daily record (Spec 4, 9.1, 9.2; B6 Entscheidungen 1-7).

Functions never commit; the caller owns the transaction.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy.bambuddy.client import BambuddyClient
from reprintbuddy.bambuddy.types import Spool
from reprintbuddy.domain import color, rules
from reprintbuddy.domain.catalog import _int_in
from reprintbuddy.domain.catalog import _text as _form_text
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import Article, ArticleFilament, FilamentSettings, FilamentSnapshot, FilamentSort

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


def _group(material: str | None, subtype: str | None) -> tuple[str, str]:
    """Spools are compared by colour only inside one material and subtype (Spec 9.1)."""
    return _norm(material), _norm(subtype)


def sort_key(material: str | None, subtype: str | None, anchor_hex: str | None) -> str:
    """Identity of a sort: material, subtype and the colour of its anchor spool. The brand is no part of
    it (Spec 9.1); another spool joins a sort by colour distance, see `nearest_sort`."""
    return "|".join((_norm(material), _norm(subtype), _norm(anchor_hex)))


def sort_label(material: str | None, subtype: str | None, color_name: str | None,
               color_hex: str | None) -> str:
    """Non-empty parts of material, subtype and colour name; the hex stands in for a missing name."""
    name = _text(color_name)
    if not name:
        last = color.display_hex(color_hex)
    elif _norm(name) == _norm(subtype):
        last = ""  # the name repeats the subtype: shown once, and no hex in its place
    else:
        last = name
    return " ".join(_text(p) for p in (material, subtype, last) if _text(p))


def nearest_sort(sorts: Iterable[FilamentSort], color_hex: str | None) -> FilamentSort | None:
    """The sort of one material and subtype whose anchor colour is nearest to `color_hex`, if it is at most
    `color.SIMILAR` away; a tie goes to the lower id. Without a usable colour only a sort without one
    (or one written alike) matches. The anchor is fixed, so a chain A~B~C of colours does not merge."""
    best: tuple[float, int] | None = None
    found: FilamentSort | None = None
    for sort in sorts:
        distance = color.delta_e(sort.color_hex, color_hex)
        if distance is None:
            distance = 0.0 if color.similar(sort.color_hex, color_hex) else None
        if distance is None or distance > color.SIMILAR:
            continue
        if best is None or (distance, sort.id) < best:
            best, found = (distance, sort.id), sort
    return found


def _sorts_by_group(session: Session) -> dict[tuple[str, str], list[FilamentSort]]:
    groups: dict[tuple[str, str], list[FilamentSort]] = defaultdict(list)
    for sort in session.scalars(select(FilamentSort).order_by(FilamentSort.id)):
        groups[_group(sort.material, sort.subtype)].append(sort)
    return groups


def resolve_sorts(session: Session, spools: Sequence[Spool]) -> dict[int, int | None]:
    """Spool id -> id of the stored sort it belongs to (None if there is none yet); writes nothing."""
    groups = _sorts_by_group(session)
    result: dict[int, int | None] = {}
    for spool in spools:
        sort = nearest_sort(groups.get(_group(spool.material, spool.subtype), ()), spool.color_hex)
        result[spool.id] = sort.id if sort is not None else None
    return result


def sync_sorts(session: Session, spools: Sequence[Spool]) -> dict[int, int]:
    """Assign every spool to a sort and create the missing ones; returns spool id -> sort id.

    Two passes, so that a second call with the same spools gives the same answer: first the anchors
    are settled (spools by ascending id; a spool with no anchor within delta E 7 becomes the anchor
    of a new sort), then every spool joins the nearest anchor of all. The anchor of a new sort is
    thus the colour of its lowest-id spool, and the outcome does not depend on the order of the list.
    The name of a sort comes from its lowest-id spool that has one; a spool without a name never
    erases a known one.
    """
    groups = _sorts_by_group(session)
    ordered = sorted(spools, key=lambda s: s.id)
    for spool in ordered:  # pass 1: the anchors
        group = groups[_group(spool.material, spool.subtype)]
        if nearest_sort(group, spool.color_hex) is None:
            sort = FilamentSort(
                sort_key=sort_key(spool.material, spool.subtype, spool.color_hex),
                brand=None,
                material=_text(spool.material),
                subtype=_text(spool.subtype) or None,
                color_hex=spool.color_hex,
                color_name=None,
                label=sort_label(spool.material, spool.subtype, None, spool.color_hex),
            )
            session.add(sort)
            session.flush()
            group.append(sort)
    assigned: dict[int, int] = {}
    members: dict[int, list[Spool]] = defaultdict(list)
    sorts: dict[int, FilamentSort] = {}
    for spool in ordered:  # pass 2: the nearest anchor of all
        sort = nearest_sort(groups[_group(spool.material, spool.subtype)], spool.color_hex)
        assigned[spool.id] = sort.id
        members[sort.id].append(spool)
        sorts[sort.id] = sort
    for sort_id, spools_of in members.items():
        named = next((sp for sp in spools_of if _text(sp.color_name)), None)
        if named is not None:
            sort = sorts[sort_id]
            sort.color_name = named.color_name
            sort.label = sort_label(sort.material, sort.subtype, named.color_name, sort.color_hex)
    session.flush()
    return assigned


def stock_by_sort(session: Session, spools: Sequence[Spool]) -> dict[int, SortStock]:
    """Live stock per sort: the summed `remaining_g` of the non-archived spools (Spec 9.1)."""
    live = [s for s in spools if not s.archived]
    ids = sync_sorts(session, live)
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
    """Stock from the rows of the latest recorded day; `({}, None)` without any record."""
    latest = session.scalar(select(func.max(FilamentSnapshot.day)))
    if latest is None:
        return {}, None
    grams: dict[int, float] = defaultdict(float)
    count: dict[int, int] = defaultdict(int)
    rows = session.scalars(select(FilamentSnapshot).where(FilamentSnapshot.day == latest))
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
    ids = sync_sorts(session, live)
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


def consumption(session: Session, sort_id: int, today: date) -> tuple[float, int]:
    """Grams per day and days of history over the last 30 days (Entscheidung 7)."""
    rows = session.execute(
        select(FilamentSnapshot.spool_id, FilamentSnapshot.day, FilamentSnapshot.remaining_g)
        .where(
            FilamentSnapshot.spool_filament_id == sort_id,
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
    """Stock of every known sort: live with `spools` (date None), else from the record.

    Sorts of the directory without stock show 0. `(None, None)`: nothing known (Anti-Ziel 4).
    """
    if spools is not None:
        stock, day = stock_by_sort(session, spools), None
    else:
        stock, day = stock_from_snapshots(session)
        if day is None:
            return None, None
    for sort in session.scalars(select(FilamentSort)):
        if sort.id not in stock:
            stock[sort.id] = _entry(sort, 0.0, 0)
    return stock, day


@dataclass(frozen=True)
class SlotRow:
    slot: int
    grams: float
    material: str | None
    color_hex: str | None
    mapped: int | None
    suggested: int | None


def suggest_sort(
    material: str | None, color_hex: str | None, sorts: Mapping[int, SortStock]
) -> int | None:
    """The one sort with the same material and a similar colour; none or several give no suggestion
    (Spec 5.2, 9.1). A slot without material or colour gets none."""
    if not _norm(material) or not _norm(color_hex):
        return None
    hits = [
        sort_id
        for sort_id, sort in sorts.items()
        if _norm(sort.material) == _norm(material) and color.similar(sort.color_hex, color_hex)
    ]
    return hits[0] if len(hits) == 1 else None


def slot_rows(session: Session, article_id: int, sorts: Mapping[int, SortStock]) -> list[SlotRow]:
    """The filament slots of an article by slot number, with stored mapping and suggestion."""
    rows = session.scalars(
        select(ArticleFilament)
        .where(ArticleFilament.article_id == article_id)
        .order_by(ArticleFilament.slot)
    )
    return [
        SlotRow(
            r.slot,
            r.grams,
            r.material,
            r.color_hex,
            r.spool_filament_id,
            suggest_sort(r.material, r.color_hex, sorts),
        )
        for r in rows
    ]


def set_mapping(session: Session, article_id: int, mapping: Mapping[int, int | None]) -> None:
    """Store `slot -> sort id` (None clears); validates everything first, other slots stay."""
    article = session.get(Article, article_id)
    if article is None:
        raise NotFoundError("articles.not_found")
    if article.archived_at is not None:
        raise ConflictError("articles.edit_archived")
    slots = {
        r.slot: r
        for r in session.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article_id))
    }
    for slot, sort_id in mapping.items():
        if slot not in slots:
            raise ValidationError("filament.unknown_slot")
        if sort_id is not None and session.get(FilamentSort, sort_id) is None:
            raise NotFoundError("filament.sort_not_found")
    for slot, sort_id in mapping.items():
        slots[slot].spool_filament_id = sort_id
    session.flush()


def assigned_sort_ids(session: Session) -> set[int]:
    """Ids of the sorts that a slot of a non-archived article uses (what the filament page lists)."""
    rows = session.scalars(
        select(ArticleFilament.spool_filament_id)
        .join(Article, Article.id == ArticleFilament.article_id)
        .where(Article.archived_at.is_(None), ArticleFilament.spool_filament_id.is_not(None))
        .distinct()
    )
    return set(rows)


def unmapped_slots(session: Session) -> list[tuple[int, str, int]]:
    """(article id, name, slot) of every slot without a sort, active articles only."""
    stmt = (
        select(Article.id, Article.name, ArticleFilament.slot)
        .join(ArticleFilament, ArticleFilament.article_id == Article.id)
        .where(Article.archived_at.is_(None), ArticleFilament.spool_filament_id.is_(None))
        .order_by(Article.name, ArticleFilament.slot)
    )
    return [(a, n, s) for a, n, s in session.execute(stmt)]


# ---- settings of a sort (Spec 9.3) -----------------------------------------------------------


@dataclass
class FilamentSettingsInput:
    lead_time_days: int = 0
    safety_value: int = 0
    safety_unit: Literal["days", "grams"] = "days"
    pack_grams: int = 1000
    min_order_packs: int = 1
    supplier: str | None = None
    order_url: str | None = None


def get_settings(session: Session, sort_id: int) -> FilamentSettings | None:
    return session.get(FilamentSettings, sort_id)


def save_settings(session: Session, sort_id: int, data: FilamentSettingsInput) -> FilamentSettings:
    """Create or replace the settings of a sort; everything is checked before anything is stored."""
    if session.get(FilamentSort, sort_id) is None:
        raise NotFoundError("filament.sort_not_found")
    if data.safety_unit not in ("days", "grams"):
        raise ValidationError("filament.invalid_safety_unit")
    lead = _int_in(data.lead_time_days, "filament.field.lead_time_days", 0, 365)
    safety = _int_in(data.safety_value, "filament.field.safety_value", 0, 1_000_000)
    pack = _int_in(data.pack_grams, "filament.field.pack_grams", 1, 100_000)
    packs = _int_in(data.min_order_packs, "filament.field.min_order_packs", 1, 1000)
    supplier = _form_text(data.supplier, "filament.field.supplier", 200)
    url = _form_text(data.order_url, "filament.field.order_url", 2000)
    if url is not None and not url.lower().startswith(("http://", "https://")):
        raise ValidationError("filament.invalid_url")
    row = session.get(FilamentSettings, sort_id)
    if row is None:
        row = FilamentSettings(spool_filament_id=sort_id)
        session.add(row)
    row.lead_time_days, row.safety_value, row.safety_unit = lead, safety, data.safety_unit
    row.pack_grams, row.min_order_packs, row.supplier, row.order_url = pack, packs, supplier, url
    session.flush()
    return row


# ---- hook in the regular tick (Entscheidung 16) ----------------------------------------------


async def poll_hook(client: BambuddyClient, sm: sessionmaker, now: datetime) -> None:
    """Record the day's spool stock and close arrived marks; errors go to the engine."""
    from reprintbuddy.domain import reorder  # reorder imports this module

    spools = await client.list_spools()  # no transaction open during the network call
    with sm.begin() as session:
        sync_sorts(session, [s for s in spools if not s.archived])
        record_snapshot(session, spools, now.date())
        reorder.close_arrived_marks(session, spools, now)
