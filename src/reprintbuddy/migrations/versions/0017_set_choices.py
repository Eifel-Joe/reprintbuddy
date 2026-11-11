"""set choices: base parts and up to two choice fields with suffixes (Spec 5.4, p340)

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-11

A set now has base parts (one article per row) and up to two choice fields: articles of which one
goes into every set, each with a suffix that makes the number of the variant. The tables stay;
`set_positions.slot` says what a position is (0 base part, 1 and 2 the choice fields, one position
each with many options), `set_position_options` gets the `suffix` and the `sort` of an option, and
`order_lines.variant_sku` keeps the number of the variant that was ordered.

The positions of v0.3.17 are converted (Spec 5.4, update paragraph): a position with one article
becomes a base part, the same article in two of them one row with the sum; the first choice
position becomes choice field 1, the second choice field 2, options sorted by name and without a
suffix (the set then shows "suffixes missing" until they are typed in). An article is only once in
a set: an option whose article is already a base part or in an earlier field is dropped, a choice
position that loses all its options goes, and the next one moves up. Choice positions beyond the
second are dropped. Whatever was dropped is named in one line of the activity log per set, from
"system". Order lines are not touched: they hold the articles chosen at the time and get no number.

Columns are added with a plain ALTER TABLE (see 0013, 0015); a new and an upgraded database come
out alike (tests/test_migration_0017_set_choices.py).
"""
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

FIELDS = 2  # choice fields per set (Spec 5.4: Wahlteile 1 and 2)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE set_positions ADD COLUMN slot INTEGER DEFAULT '0' NOT NULL"
        " CONSTRAINT ck_set_positions_slot CHECK (slot IN (0, 1, 2))"
    )
    op.add_column("set_position_options", sa.Column("suffix", sa.String(), nullable=True))
    op.add_column("set_position_options", sa.Column("sort", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("order_lines", sa.Column("variant_sku", sa.String(), nullable=True))
    _convert(op.get_bind())
    op.create_index(
        "uq_set_positions_slot", "set_positions", ["set_id", "slot"], unique=True,
        sqlite_where=sa.text("slot > 0"),
    )


def _convert(conn) -> None:
    names = dict(conn.execute(sa.text("SELECT id, name FROM articles")).all())
    by_name = lambda article_id: (str(names.get(article_id, "")).casefold(), article_id)  # noqa: E731
    now = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    for set_id, set_name in conn.execute(sa.text("SELECT id, name FROM product_sets ORDER BY id")).all():
        positions = conn.execute(
            sa.text("SELECT id, qty FROM set_positions WHERE set_id = :s ORDER BY sort, id"), {"s": set_id}
        ).all()
        options = {
            pid: [a for (a,) in conn.execute(
                sa.text("SELECT article_id FROM set_position_options WHERE position_id = :p ORDER BY article_id"),
                {"p": pid},
            )]
            for pid, _qty in positions
        }
        base: dict[int, int] = {}  # article id -> its base position
        choices: list[int] = []
        for pid, qty in positions:
            ids = options[pid]
            if len(ids) > 1:
                choices.append(pid)
            elif len(ids) == 1 and ids[0] in base:  # the same fixed article twice: one row, the sum
                conn.execute(sa.text("UPDATE set_positions SET qty = qty + :q WHERE id = :p"),
                             {"q": qty, "p": base[ids[0]]})
                _delete(conn, pid)
            elif len(ids) == 1:
                base[ids[0]] = pid
        seen = set(base)
        dropped: set[int] = set()
        kept = 0
        for pid in choices:
            remaining = [a for a in options[pid] if a not in seen]
            if not remaining or kept == FIELDS:
                dropped.update(options[pid])
                _delete(conn, pid)
                continue
            for article_id in options[pid]:
                if article_id in seen:
                    dropped.add(article_id)
                    conn.execute(
                        sa.text("DELETE FROM set_position_options WHERE position_id = :p AND article_id = :a"),
                        {"p": pid, "a": article_id},
                    )
            kept += 1
            conn.execute(sa.text("UPDATE set_positions SET slot = :slot WHERE id = :p"), {"slot": kept, "p": pid})
            for sort, article_id in enumerate(sorted(remaining, key=by_name)):
                conn.execute(
                    sa.text("UPDATE set_position_options SET sort = :sort WHERE position_id = :p AND article_id = :a"),
                    {"sort": sort, "p": pid, "a": article_id},
                )
            seen.update(remaining)
        if dropped:
            details = {"id": set_id, "name": set_name,
                       "dropped": [str(names.get(a, a)) for a in sorted(dropped, key=by_name)]}
            conn.execute(
                sa.text("INSERT INTO activity (at, area, kind, actor, details)"
                        " VALUES (:at, 'master', 'set_migrated', 'system', :details)"),
                {"at": now, "details": json.dumps(details)},
            )


def _delete(conn, position_id: int) -> None:
    conn.execute(sa.text("DELETE FROM set_position_options WHERE position_id = :p"), {"p": position_id})
    conn.execute(sa.text("DELETE FROM set_positions WHERE id = :p"), {"p": position_id})


def downgrade() -> None:
    """Choice fields become positions with a choice again; numbers and the order of options go."""
    op.drop_index("uq_set_positions_slot", table_name="set_positions")
    op.execute("ALTER TABLE order_lines DROP COLUMN variant_sku")
    op.execute("ALTER TABLE set_position_options DROP COLUMN sort")
    op.execute("ALTER TABLE set_position_options DROP COLUMN suffix")
    op.execute("ALTER TABLE set_positions DROP COLUMN slot")
