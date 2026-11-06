from datetime import datetime, timedelta

import pytest

from factories import make_filament_settings
from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.domain import filament, reorder
from reprintbuddy.models import FilamentSnapshot, FilamentSort, ReorderMark

NOW = datetime(2026, 11, 6, 12, 0)


async def test_hook_records_and_closes(db, fake_bambuddy):
    fake = fake_bambuddy
    old = fake.add_spool("PLA", created_at=NOW - timedelta(days=9))
    with db.begin() as s:
        filament.sync_sorts(s, [old])
        sort = s.query(FilamentSort).one()
        make_filament_settings(s, sort.id, pack_grams=1000)
        mark = reorder.mark_ordered(s, "filament", sort.id, 1)
        mark.marked_at = NOW - timedelta(days=1)
    fake.add_spool("PLA", created_at=NOW - timedelta(hours=2))

    await filament.poll_hook(fake, db, NOW)

    with db() as s:
        assert s.query(FilamentSort).count() == 1
        assert s.query(FilamentSnapshot).filter_by(day=NOW.date()).count() == 2
        assert s.query(ReorderMark).one().received_at == NOW


async def test_hook_unavailable_raises(db, fake_bambuddy):
    fake_bambuddy.unavailable = True
    with pytest.raises(BambuddyUnavailable):
        await filament.poll_hook(fake_bambuddy, db, NOW)


async def test_hook_runs_in_engine_tick(engine_env):
    env = engine_env
    env.fake.add_spool("PLA")
    env.fake.add_spool("PETG", color_hex="#FFFFFF")
    env.engine.add_poll_hook("filament", filament.poll_hook)

    await env.engine.tick(poll=True)

    assert "filament" not in env.engine.status.hook_errors
    with env.sm() as s:
        assert s.query(FilamentSort).count() == 2
        assert s.query(FilamentSnapshot).count() == 2
