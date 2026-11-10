"""The pages that show filament stock while writes are paused or Bambuddy is known to be down (v0.3.15).

p230: the reorder page, the filament page and the filament mapping of an article wrote when opened (new
spool sorts, arrived "ordered" marks closed), also while writes are paused for a restore or an update. After
a restore the old process could so write into the restored database before its restart. They now read only
then, as the overview does since v0.3.14 (the rule of the activity log, `may_write`).

p231: they asked Bambuddy for the spools even when the last query found it unreachable, and waited for the
timeout before showing the recorded stock. They now skip the question then, as the overview does.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from conftest import sm_of
from factories import assign_sort, make_filament_settings, make_part, make_snapshot, make_sort

from reprintbuddy.domain import stock
from reprintbuddy.models import ArticleFilament, FilamentSort, ReorderMark, utcnow

PAGES = ("/reorder", "/filament", "/articles/{article}/filaments")


def seeded(client, fake) -> dict[str, int]:
    """A reordered spool sort with yesterday's record, a spool of a sort not known yet (opening a page
    that may write enters it), and an "ordered" part mark whose receipt is booked (closed when written)."""
    yesterday = utcnow().date() - timedelta(days=1)
    fake.add_spool("PETG", color_hex="#FF0000", color_name="Red", remaining_g=500.0)
    with sm_of(client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, pack_grams=1000, safety_value=2000, safety_unit="grams")
        article = assign_sort(s, sort)
        make_snapshot(s, yesterday, 2, sort.id, 640.0)
        part = make_part(s, stock=0, pack_size=10)
        s.add(ReorderMark(item_type="part", item_id=part.id, qty=1, marked_at=utcnow() - timedelta(days=2)))
        s.flush()
        stock.receive_part(s, part.id, 10, "user")
        return {"article": article.id}


def data_version(client) -> int:
    """SQLite's counter of commits by other connections: unchanged means nobody wrote meanwhile."""
    return int(client._probe.execute("PRAGMA data_version").fetchone()[0])


@pytest.fixture
def probed(bb_client):
    db_path = bb_client.app.state.db_path
    bb_client._probe = sqlite3.connect(db_path)
    yield bb_client
    bb_client._probe.close()


@pytest.mark.parametrize("path", PAGES)
def test_nothing_is_written_while_writes_are_paused(probed, fake_bambuddy, monkeypatch, path):
    """p230. During a restore or an update the pages read the record only and write nothing."""
    ids = seeded(probed, fake_bambuddy)
    sorts_before = len(sm_of(probed)().query(FilamentSort).all())
    monkeypatch.setattr(probed.app.state.activity, "may_write", lambda: False)
    fake_bambuddy.calls.clear()
    before = data_version(probed)
    page = probed.get(path.format(**ids))
    assert page.status_code == 200
    assert data_version(probed) == before
    assert "list_spools" not in fake_bambuddy.calls  # live spools would enter new sorts: not asked
    with sm_of(probed)() as s:
        assert len(s.query(FilamentSort).all()) == sorts_before
        assert s.query(ReorderMark).one().received_at is None  # stays "on its way" until a page may write
    if path != "/reorder":
        assert "640" in page.text  # the recorded stock


def test_the_pages_still_write_when_writes_are_allowed(probed, fake_bambuddy):
    """The counter-check: unpaused, opening the reorder page enters the new sort and closes the mark."""
    seeded(probed, fake_bambuddy)
    before = data_version(probed)
    assert probed.get("/reorder").status_code == 200
    assert data_version(probed) != before
    with sm_of(probed)() as s:
        assert s.query(ReorderMark).one().received_at is not None
        assert any(sort.material == "PETG" for sort in s.query(FilamentSort))


def test_the_slot_of_the_mapping_page_is_there(bb_client, fake_bambuddy):
    """The article of `seeded` has the slot the mapping page lists (guards the fixtures above)."""
    ids = seeded(bb_client, fake_bambuddy)
    with sm_of(bb_client)() as s:
        assert s.query(ArticleFilament).filter_by(article_id=ids["article"]).count() == 1
