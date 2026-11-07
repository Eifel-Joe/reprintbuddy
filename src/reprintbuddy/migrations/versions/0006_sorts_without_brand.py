"""filament sorts without the brand: sorts of one material, subtype and similar colour become one (Spec 9.1)

Until now a sort was brand + material + subtype + colour. From now on it is material + subtype +
a colour within delta E 7 (CIEDE2000) of the sort's anchor colour. Existing sorts are merged by the
same rule, the lowest id wins and keeps the anchor colour. What pointed at a merged sort (daily
records, "ordered" marks, article slots, settings) is moved to the sort that stays. If two sorts
with settings fall together, the settings of the lower id stay and the others are dropped; every
merge is written to the journal with the dropped settings.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-07
"""
import json
from types import SimpleNamespace

import sqlalchemy as sa
from alembic import op

from reprintbuddy.domain import color

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_SIMILAR = 7.0  # frozen: the threshold of Spec 9.1 as it was when this revision was written
_SETTINGS = (
    "lead_time_days", "safety_value", "safety_unit", "pack_grams", "min_order_packs", "supplier",
    "order_url",
)


def _norm(value):
    return (value or "").strip().casefold()


def _text(value):
    return (value or "").strip()


def _key(material, subtype, anchor):
    return "|".join((_norm(material), _norm(subtype), _norm(anchor)))


def _label(material, subtype, name, hex_value):
    name = _text(name)
    if not name:
        last = color.display_hex(hex_value)
    elif _norm(name) == _norm(subtype):
        last = ""
    else:
        last = name
    return " ".join(_text(p) for p in (material, subtype, last) if _text(p))


def _nearest(kept, color_hex):
    """The kept sort of the group nearest to `color_hex` within delta E 7; a tie goes to the lower id."""
    best, found = None, None
    for sort in kept:
        distance = color.distance(sort.color_hex, color_hex)
        if distance is None or distance > _SIMILAR:
            continue
        if best is None or (distance, sort.id) < best:
            best, found = (distance, sort.id), sort
    return found


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, material, subtype, color_hex, color_name FROM filament_sorts ORDER BY id")
    ).all()
    groups = {}
    kept_sorts = []
    for row in rows:  # pass 1: the keepers, lowest id first (as `sync_sorts` settles its anchors)
        sort = SimpleNamespace(**row._mapping)
        group = groups.setdefault((_norm(sort.material), _norm(sort.subtype)), [])
        if _nearest(group, sort.color_hex) is None:
            group.append(sort)
            kept_sorts.append(sort)
    merged = {sort.id: [] for sort in kept_sorts}
    for row in rows:  # pass 2: every other sort joins the nearest keeper of all
        sort = SimpleNamespace(**row._mapping)
        target = _nearest(groups[(_norm(sort.material), _norm(sort.subtype))], sort.color_hex)
        if target.id != sort.id:
            merged[target.id].append(sort)

    for keeper in kept_sorts:
        others = merged[keeper.id]
        if others:
            _merge(bind, keeper, others)
        name = keeper.color_name if _text(keeper.color_name) else next(
            (o.color_name for o in others if _text(o.color_name)), keeper.color_name
        )
        bind.execute(
            sa.text(
                "UPDATE filament_sorts SET sort_key = :key, brand = NULL, color_name = :name, label = :label"
                " WHERE id = :id"
            ),
            {
                "key": _key(keeper.material, keeper.subtype, keeper.color_hex),
                "name": name,
                "label": _label(keeper.material, keeper.subtype, name, keeper.color_hex),
                "id": keeper.id,
            },
        )


def _merge(bind, keeper, others) -> None:
    ids = [o.id for o in others]
    marks = ", ".join(f":m{i}" for i in range(len(ids)))
    params = {f"m{i}": value for i, value in enumerate(ids)}
    settings = {
        r.spool_filament_id: r
        for r in bind.execute(
            sa.text(f"SELECT * FROM filament_settings WHERE spool_filament_id IN (:k, {marks})"),
            {"k": keeper.id, **params},
        )
    }
    holder = keeper.id if keeper.id in settings else next((i for i in ids if i in settings), None)
    dropped = []
    for sort_id, row in settings.items():
        if sort_id == holder:
            continue
        dropped.append({"sort": sort_id, **{f: getattr(row, f) for f in _SETTINGS}})
    if holder is not None and holder != keeper.id:  # the keeper has none: the lowest merged one moves in
        bind.execute(
            sa.text("UPDATE filament_settings SET spool_filament_id = :k WHERE spool_filament_id = :h"),
            {"k": keeper.id, "h": holder},
        )
    bind.execute(sa.text(f"DELETE FROM filament_settings WHERE spool_filament_id IN ({marks})"), params)
    for table, column, extra in (
        ("filament_snapshots", "spool_filament_id", ""),
        ("article_filaments", "spool_filament_id", ""),
        ("reorder_marks", "item_id", " AND item_type = 'filament'"),
    ):
        bind.execute(
            sa.text(f"UPDATE {table} SET {column} = :k WHERE {column} IN ({marks}){extra}"),
            {"k": keeper.id, **params},
        )
    _join_open_marks(bind, keeper.id)
    bind.execute(sa.text(f"DELETE FROM filament_sorts WHERE id IN ({marks})"), params)
    note = f"Filament sorts {', '.join(map(str, ids))} merged into {keeper.id} (brand no longer counts)"
    moved = holder if holder is not None and holder != keeper.id else None
    if moved is not None:
        note += f"; settings of {moved} kept"
    if dropped:
        note += "; settings dropped: " + ", ".join(str(d["sort"]) for d in dropped)
    bind.execute(
        sa.text(
            "INSERT INTO journal (at, actor, kind, qty, note, details)"
            " VALUES (CURRENT_TIMESTAMP, 'auto', 'filament_sorts_merged', 0, :note, :details)"
        ),
        {
            "note": note,
            "details": json.dumps(
                {"into": keeper.id, "from": ids, "settings_from": moved, "dropped_settings": dropped}, ensure_ascii=False
            ),
        },
    )


def _join_open_marks(bind, keeper_id) -> None:
    """Two open "ordered" marks of one sort would each count as unfinished goods: they become one
    (the order units add up, the earlier day stays)."""
    open_marks = bind.execute(
        sa.text(
            "SELECT id, qty, marked_at FROM reorder_marks"
            " WHERE item_type = 'filament' AND item_id = :k AND received_at IS NULL ORDER BY marked_at, id"
        ),
        {"k": keeper_id},
    ).all()
    if len(open_marks) < 2:
        return
    first = open_marks[0]
    bind.execute(
        sa.text("UPDATE reorder_marks SET qty = :qty WHERE id = :id"),
        {"qty": sum(m.qty for m in open_marks), "id": first.id},
    )
    for mark in open_marks[1:]:
        bind.execute(sa.text("DELETE FROM reorder_marks WHERE id = :id"), {"id": mark.id})


def downgrade() -> None:
    pass  # merged sorts cannot be told apart again
