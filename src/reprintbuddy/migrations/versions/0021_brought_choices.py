"""inner sets bring their choice parts: the fixed variant goes (Spec 5.4, 0.4.4 p441)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-12

Since 0.4.4 an inner set brings its choice fields into the outer set and the choice is made when
ordering (Spec 5.4 "Mitgebrachte Wahlteile"), so the fixed variant of an inner set in
`set_position_sets.choice1_id/choice2_id` goes. SQLite cannot drop a column with a foreign key, so
the table is rebuilt with written-out SQL. Rejected: batch mode (as in 0007): it reflects the foreign
keys in no fixed order, so an upgraded and a new database came out with them in different orders.

The update names, in one activity line per set from "system", every set whose variants now arise
anew: a set that holds, on any level, an inner set row with a fixed variant ("inner": the sets whose
fixed variant ended). Since every option of a brought field now counts against an article twice, a
set may now hold an article twice that 0.4.3 allowed (an option its fixed variant did not choose);
the line names it ("twice") and the set shows "broken" until it is changed (plan v0.4.4, F3). A set
that now brings choice parts but has no number shows "number missing" (Spec 5.4 update paragraph).
Orders are not touched: their lines and inner paths hold what was ordered.

Downgrade gives the columns back and fixes each inner set row on the first option of each own choice
field of the inner set, so 0.4.3 reads every set again.
Tests: tests/test_migration_0021_brought_choices.py.
"""
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    _log(conn)
    op.execute(
        "CREATE TABLE _set_position_sets_new ("
        " position_id INTEGER NOT NULL, set_id INTEGER NOT NULL, PRIMARY KEY (position_id),"
        " FOREIGN KEY(position_id) REFERENCES set_positions (id),"
        " FOREIGN KEY(set_id) REFERENCES product_sets (id))"
    )
    op.execute("INSERT INTO _set_position_sets_new (position_id, set_id) SELECT position_id, set_id FROM set_position_sets")
    op.execute("DROP TABLE set_position_sets")
    op.execute("ALTER TABLE _set_position_sets_new RENAME TO set_position_sets")


def _log(conn) -> None:
    """One line per set whose variants arise anew; worked out from the stored rows, without the
    application's code, so the update stays as it is when the code changes later."""
    names = dict(conn.execute(sa.text("SELECT id, name FROM product_sets")).all())
    articles = dict(conn.execute(sa.text("SELECT id, name FROM articles")).all())
    inner: dict[int, list[tuple[int, bool]]] = defaultdict(list)  # set -> (inner set, had a fixed variant)
    for set_id, inner_id, choice1, choice2 in conn.execute(sa.text(
        "SELECT p.set_id, s.set_id, s.choice1_id, s.choice2_id FROM set_position_sets s"
        " JOIN set_positions p ON p.id = s.position_id ORDER BY p.set_id, p.sort, p.id"
    )):
        inner[set_id].append((inner_id, choice1 is not None or choice2 is not None))
    own: dict[int, list[int]] = defaultdict(list)  # articles of the own rows: base parts and every option
    for set_id, article_id in conn.execute(sa.text(
        "SELECT p.set_id, o.article_id FROM set_positions p JOIN set_position_options o ON o.position_id = p.id"
    )):
        own[set_id].append(article_id)

    def walk(set_id: int, trail: tuple[int, ...]) -> tuple[list[int], list[int]]:
        """(every article the set may hold now, the sets on any level whose fixed variant ended)."""
        found = list(own[set_id])
        ended: list[int] = []
        for inner_id, fixed in inner[set_id]:
            if inner_id in trail:
                continue  # a ring cannot be stored through the forms; nothing to name
            if fixed and inner_id not in ended:
                ended.append(inner_id)
            more, deeper = walk(inner_id, trail + (inner_id,))
            found += more
            ended += [i for i in deeper if i not in ended]
        return found, ended

    now = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    for set_id in sorted(names):
        found, ended = walk(set_id, (set_id,))
        if not ended:
            continue
        counts = Counter(found)
        twice = sorted((str(articles.get(a, a)) for a, n in counts.items() if n > 1), key=str.casefold)
        details = {
            "id": set_id, "name": names[set_id],
            "inner": sorted((str(names.get(i, i)) for i in ended), key=str.casefold), "twice": twice,
        }
        conn.execute(
            sa.text("INSERT INTO activity (at, area, kind, actor, details)"
                    " VALUES (:at, 'master', 'set_choices_brought', 'system', :details)"),
            {"at": now, "details": json.dumps(details)},
        )


def downgrade() -> None:
    op.execute("ALTER TABLE set_position_sets ADD COLUMN choice1_id INTEGER REFERENCES articles (id)")
    op.execute("ALTER TABLE set_position_sets ADD COLUMN choice2_id INTEGER REFERENCES articles (id)")
    conn = op.get_bind()
    for slot, column in ((1, "choice1_id"), (2, "choice2_id")):
        conn.execute(sa.text(
            f"UPDATE set_position_sets SET {column} = ("
            " SELECT o.article_id FROM set_positions p JOIN set_position_options o ON o.position_id = p.id"
            f" WHERE p.set_id = set_position_sets.set_id AND p.slot = {slot}"
            " ORDER BY o.sort, o.article_id LIMIT 1)"
        ))
