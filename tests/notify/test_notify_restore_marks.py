"""The marks of the push messages carried over a restore (p220, review v0.3.15 D, findings 1 and 2).

`restore_notify_marks` runs at the start after a restore, on the restored (and migrated) database.
- Finding 1: ids are not kept for ever (SQLite hands out max(id)+1). A mark of a print, an article or a
  part that only the replaced database had would mute the first new thing that gets the same id in the
  restored one. Such marks are left out.
- Finding 2: switched off, the push messages keep no marks (`forget_all`), and switching them on relies
  on that. The marks are carried over only when the messages are on in both databases; otherwise the
  restored database keeps its own.
"""

from __future__ import annotations

import sqlite3

from factories import make_article, make_dispatch, make_part, make_sort
from sqlalchemy import select

from reprintbuddy import settings as st
from reprintbuddy.activity.writer import current_notify_marks, restore_notify_marks
from reprintbuddy.models import NotifyMark


def mark(kind: str, ref: str, state: str = "sent") -> dict:
    return {
        "kind": kind, "ref": ref, "state": state, "streak": 0,
        "first_seen_at": "2026-11-08T08:00:00", "notified_at": "2026-11-08T08:01:00", "gone_since": None,
    }


def marks_of(db) -> dict[tuple[str, str], str]:
    with db() as s:
        return {(m.kind, m.ref): m.state for m in s.scalars(select(NotifyMark))}


def switched_on(db) -> None:
    with db.begin() as s:
        st.set_setting(s, "ha_enabled", True, commit=False)


def test_marks_of_things_the_restored_database_does_not_have_are_left_out(db):
    """Finding 1: print 2 and article/part/sort 99 exist only in the replaced database. Their marks would
    mute the next print, article, part or sort that gets the same id."""
    switched_on(db)
    with db.begin() as s:
        article = make_article(s)
        dispatch = make_dispatch(s, article.id, status="completed")
        part = make_part(s)
        sort = make_sort(s)
        ids = {"article": article.id, "dispatch": dispatch.id, "part": part.id, "sort": sort.id}
    carried = [
        mark("print_done", str(ids["dispatch"])),
        mark("print_failed", str(ids["dispatch"] + 1)),
        mark("below_min", str(ids["article"])),
        mark("paused", "99"),
        mark("reorder", f"part:{ids['part']}"),
        mark("reorder", "part:99"),
        mark("reorder", f"filament:{ids['sort']}"),
        mark("reorder", "filament:99"),
        mark("trouble", "bambuddy_unreachable", "due"),
        mark("rights", "bambuddy"),
    ]
    restore_notify_marks(db, carried)
    assert marks_of(db) == {
        ("print_done", str(ids["dispatch"])): "sent",
        ("below_min", str(ids["article"])): "sent",
        ("reorder", f"part:{ids['part']}"): "sent",
        ("reorder", f"filament:{ids['sort']}"): "sent",
        ("trouble", "bambuddy_unreachable"): "due",
        ("rights", "bambuddy"): "sent",
    }


def test_nothing_is_carried_into_a_database_with_the_messages_switched_off(db):
    """Finding 2b: the backup had the messages off. Its table stays as it is (empty), so switching them on
    later reports what holds then, once."""
    with db.begin() as s:
        article = make_article(s)
    restore_notify_marks(db, [mark("below_min", str(article.id))])
    assert marks_of(db) == {}


def test_a_replaced_database_with_the_messages_off_leaves_no_marks(tmp_path, db):
    """Finding 2a: switched off, the replaced database has no marks because nothing is followed. Carrying
    that empty list would delete the marks of the backup, whose settings switch the messages on again:
    everything the backup had reported would go out once more. So nothing is carried (None)."""
    from reprintbuddy.db import make_engine, upgrade

    path = tmp_path / "replaced.db"
    engine = make_engine(path)
    upgrade(engine)
    engine.dispose()
    assert current_notify_marks(path) is None  # the default: off
    con = sqlite3.connect(path)
    with con:
        con.execute("INSERT INTO settings (key, value) VALUES ('ha_enabled', 'true')")
    con.close()
    assert current_notify_marks(path) == []
