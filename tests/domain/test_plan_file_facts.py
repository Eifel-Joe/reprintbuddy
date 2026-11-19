"""Print time and nozzle of the files waiting in Reprintbuddy's own queue, for the print plan (Spec 7.6,
10.2, p646): read with the regular query, kept in memory, asked again only for a new or changed file."""

import dataclasses

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.domain.printerboard import FileFacts
from reprintbuddy.models import Article


def _stop_queue(env) -> None:
    """Entries stay in Reprintbuddy's queue: nothing is handed over."""
    with env.sm.begin() as s:
        st.set_setting(s, "queue_running", False)


def _file(env, file_id: int):
    return env.fake._files[file_id]


async def test_poll_reads_plate_times_for_files_in_the_own_queue(engine_env):
    env = engine_env  # Template A: file 1, plate 1, 3600 s in the fake
    env.fake._files[1] = dataclasses.replace(_file(env, 1), nozzle_diameter=0.4)
    _stop_queue(env)

    await env.engine.tick(poll=True)  # the rule queues a print, the poll reads its file

    assert env.engine.board.file_facts == {(1, "v1"): FileFacts(nozzle=0.4, plate_seconds={1: 3600})}


async def test_poll_asks_plates_only_for_new_or_changed_files(engine_env):
    env = engine_env
    _stop_queue(env)
    await env.engine.tick(poll=True)
    assert env.fake.calls.count("list_plates") == 1

    await env.engine.tick(poll=True)
    assert env.fake.calls.count("list_plates") == 1  # known: no request

    # A new version of the file, taken over by the article (as a confirmed change does).
    env.fake._files[1] = dataclasses.replace(_file(env, 1), fingerprint="v2")
    with env.sm.begin() as s:
        s.get(Article, env.article_id).file_fingerprint = "v2"
    await env.engine.tick(poll=True)

    assert env.fake.calls.count("list_plates") == 2
    assert set(env.engine.board.file_facts) == {(1, "v2")}  # the old version is dropped


async def test_poll_without_own_queue_asks_no_plates(engine_env):
    env = engine_env
    with env.sm.begin() as s:
        st.set_setting(s, "automation", False)  # the rule queues nothing

    await env.engine.tick(poll=True)

    assert "list_plates" not in env.fake.calls
    assert env.engine.board.file_facts == {}


async def test_plate_read_failure_changes_nothing_else(engine_env):
    env = engine_env
    _stop_queue(env)
    env.fake.fail_next("list_plates", BambuddyRejected("no right"))

    await env.engine.tick(poll=True)

    assert env.engine.status.poll_error is None
    assert env.engine.status.reachable is True
    assert env.engine.board.printers is not None
    assert env.engine.board.file_facts == {}

    await env.engine.tick(poll=True)  # tried again with the next query
    assert (1, "v1") in env.engine.board.file_facts


async def test_plate_read_unreachable_is_tried_again(engine_env):
    env = engine_env
    _stop_queue(env)
    env.fake.fail_next("list_plates", BambuddyUnavailable("gone"))

    await env.engine.tick(poll=True)
    assert env.engine.board.file_facts == {}

    await env.engine.tick(poll=True)
    assert env.engine.board.file_facts[(1, "v1")].plate_seconds == {1: 3600}


async def test_new_facts_are_published_unchanged_are_not(engine_env, monkeypatch):
    env = engine_env
    _stop_queue(env)
    sent: list[str] = []
    monkeypatch.setattr(env.engine.board, "facts_changed", lambda: sent.append("facts"))

    await env.engine.tick(poll=True)
    assert sent == ["facts"]

    await env.engine.tick(poll=True)
    assert sent == ["facts"]  # nothing new


async def test_a_short_tick_reads_no_plates(engine_env):
    """Only the regular query reads them (Spec 7.6); a short tick after a write does not ask Bambuddy."""
    env = engine_env
    _stop_queue(env)
    await env.engine.tick(poll=False)
    assert "list_plates" not in env.fake.calls
