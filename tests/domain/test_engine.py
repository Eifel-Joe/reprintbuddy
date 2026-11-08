"""The engine's tick: rule 7.1, hand-over and polling under one lock (plan B4 Task 10).

Decisions 4, 5, 12, 14-17; review focus 2, 3, 5. Two ticks never interleave; an unclear,
unreachable or rejecting Bambuddy gets nothing new; during an update nothing happens.
"""

import asyncio
import threading
import contextlib
import time

from sqlalchemy import select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.domain.engine import (
    Engine,
    EngineStatus,
    EngineWarning,
    TickResult,
    collect_warnings,
)
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.models import Article, Dispatch
from reprintbuddy.i18n import translate


# ---- helpers ---------------------------------------------------------------------------


def setting(sm, key, value):
    with sm() as s:
        st.set_setting(s, key, value)


def queue_of(sm):
    with sm() as s:
        return [(e.article_id, e.prints, e.origin) for e in wq.list_entries(s)]


def statuses(sm):
    with sm() as s:
        return [d.status for d in s.scalars(select(Dispatch).order_by(Dispatch.id))]


def item_ids(sm):
    with sm() as s:
        return [d.bambuddy_item_id for d in s.scalars(select(Dispatch).order_by(Dispatch.id))]


def warnings(sm, status):
    with sm() as s:
        return collect_warnings(s, status)


def keys(sm, status):
    return [w.key for w in warnings(sm, status)]


def update_article(sm, article_id, **fields):
    with sm.begin() as s:
        article = s.get(Article, article_id)
        for name, value in fields.items():
            setattr(article, name, value)


def second_article(sm):
    """Template B (file 2 of the fake, plate 1), same rule as Template A."""
    with sm.begin() as s:
        return make_article(
            s,
            name="Template B",
            library_file_id=2,
            plate_id=1,
            file_name="Template B.gcode.3mf",
            file_fingerprint="v1",
            target_model="A1",
            auto_enabled=True,
            min_stock=1,
        ).id


def never_sent(sm, article_id):
    """A print reserved but never sent (`sending`, no `enqueue_sent_at`), e.g. after a crash."""
    with sm.begin() as s:
        return make_dispatch(s, article_id, status="sending").id


async def wait_until(condition, timeout=1.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not met in time"
        await asyncio.sleep(0.002)


@contextlib.asynccontextmanager
async def running(engine, poll_seconds):
    task = asyncio.create_task(engine.run(poll_seconds))
    try:
        yield task
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def record_ticks(engine, monkeypatch, *, fail_first=False):
    calls = []

    async def tick(*, poll):
        calls.append(poll)
        if fail_first and len(calls) == 1:
            raise RuntimeError("boom")
        return TickResult(None, 0, 0)

    monkeypatch.setattr(engine, "tick", tick)
    return calls


# ---- rule and hand-over ----------------------------------------------------------------


async def test_tick_rule_and_dispatch(engine_env):
    env = engine_env

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 1)
    assert queue_of(env.sm) == []
    assert statuses(env.sm) == ["queued"]
    assert len(await env.fake.list_queue()) == 1
    assert env.engine.status.reachable is True
    assert env.engine.status.configured is True
    assert env.engine.status.last_tick_at is not None


async def test_second_tick_adds_nothing(engine_env):
    env = engine_env
    await env.engine.tick(poll=False)

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 0)
    assert len(env.fake.enqueued) == 1
    assert queue_of(env.sm) == []


async def test_completion_no_reprint(engine_env):
    env = engine_env
    await env.engine.tick(poll=False)
    (item_id,) = item_ids(env.sm)
    env.fake.set_status(item_id, "completed")

    result = await env.engine.tick(poll=True)

    assert result == TickResult(None, 0, 0)
    assert statuses(env.sm) == ["completed"]
    with env.sm() as s:
        assert s.get(Article, env.article_id).provisional == 1
    assert queue_of(env.sm) == []
    assert len(env.fake.enqueued) == 1
    assert env.engine.status.last_poll_at is not None


async def test_queue_stopped_creates_but_does_not_dispatch(engine_env):
    env = engine_env
    setting(env.sm, "queue_running", False)

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 0)
    assert "enqueue" not in env.fake.calls
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    assert "warning.queue_stopped" in keys(env.sm, env.engine.status)
    assert env.engine.status.cap_reached is False


async def test_cap_one(engine_env):
    env = engine_env
    b = second_article(env.sm)
    setting(env.sm, "queue_cap", 1)

    first = await env.engine.tick(poll=False)
    assert first == TickResult(None, 2, 1)
    assert len(await env.fake.list_queue()) == 1
    assert env.engine.status.cap_reached is True

    again = await env.engine.tick(poll=False)
    assert again == TickResult(None, 0, 0)
    assert len(env.fake.enqueued) == 1
    assert env.engine.status.cap_reached is True
    assert EngineWarning("warning.cap_reached", {"cap": 1}) in warnings(
        env.sm, env.engine.status
    )

    (item_id,) = item_ids(env.sm)
    env.fake.set_status(item_id, "completed")
    last = await env.engine.tick(poll=True)

    assert last == TickResult(None, 0, 1)
    assert len(env.fake.enqueued) == 2
    assert statuses(env.sm) == ["completed", "queued"]
    assert queue_of(env.sm) == []
    with env.sm() as s:
        assert s.get(Dispatch, 2).article_id == b
    assert env.engine.status.cap_reached is False


async def test_cap_lowered_recalls_nothing(engine_env):
    env = engine_env
    update_article(env.sm, env.article_id, min_stock=3)
    await env.engine.tick(poll=False)
    assert statuses(env.sm) == ["queued"] * 3
    setting(env.sm, "queue_cap", 1)
    update_article(env.sm, env.article_id, min_stock=4)
    env.fake.calls.clear()

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 0)
    assert "enqueue" not in env.fake.calls
    assert statuses(env.sm) == ["queued"] * 3
    assert len(await env.fake.list_queue()) == 3
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    assert env.engine.status.cap_reached is True


async def test_concurrent_ticks_hand_over_once(engine_env):
    env = engine_env
    env.fake.delay = 0.05

    await asyncio.gather(env.engine.tick(poll=False), env.engine.tick(poll=False))

    assert len(env.fake.enqueued) == 1
    assert queue_of(env.sm) == []
    assert statuses(env.sm) == ["queued"]


async def test_concurrent_ticks_respect_cap(engine_env):
    env = engine_env
    update_article(env.sm, env.article_id, min_stock=5)
    setting(env.sm, "queue_cap", 2)
    env.fake.delay = 0.01

    await asyncio.gather(*(env.engine.tick(poll=False) for _ in range(3)))

    assert statuses(env.sm) == ["queued", "queued"]
    assert len(env.fake.enqueued) == 2
    assert queue_of(env.sm) == [(env.article_id, 3, "rule")]


async def test_tick_skipped_during_update(engine_env):
    env = engine_env
    env.updating = True

    result = await env.engine.tick(poll=True)

    assert result == TickResult("update", 0, 0)
    assert env.fake.calls == []
    assert queue_of(env.sm) == []
    assert env.engine.status.skipped_for_update is True
    assert "warning.update_running" in keys(env.sm, env.engine.status)

    env.updating = False
    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 1)
    assert env.engine.status.skipped_for_update is False


async def test_update_between_reservation_and_send(engine_env, monkeypatch):
    """Final review 1: the update's backup may already be taken; nothing may be sent now."""
    env = engine_env
    setting(env.sm, "insert_next", True)  # Bambuddy is asked (list_queue) before the send
    list_queue = env.fake.list_queue

    async def read_then_update():
        items = await list_queue()
        env.updating = True
        return items

    monkeypatch.setattr(env.fake, "list_queue", read_then_update)

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 0)
    assert "enqueue" not in env.fake.calls
    assert env.fake.enqueued == []
    assert statuses(env.sm) == ["returned"]
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    assert env.engine.status.reachable is True
    assert env.engine.status.skipped_for_update is True


async def test_update_between_two_prints_of_one_tick(engine_env, monkeypatch):
    env = engine_env
    update_article(env.sm, env.article_id, min_stock=2)
    enqueue = env.fake.enqueue

    async def enqueue_then_update(req):
        item = await enqueue(req)
        env.updating = True
        return item

    monkeypatch.setattr(env.fake, "enqueue", enqueue_then_update)

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 1)
    assert env.fake.calls.count("enqueue") == 1
    assert len(env.fake.enqueued) == 1
    assert statuses(env.sm) == ["queued"]  # the second print was never reserved
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    assert env.engine.status.reachable is True
    assert env.engine.status.skipped_for_update is True


# ---- Bambuddy unreachable, unclear or refusing -----------------------------------------


async def test_unreachable_warns_and_keeps_stock(engine_env):
    env = engine_env
    env.fake.unavailable = True

    result = await env.engine.tick(poll=True)

    assert result == TickResult(None, 1, 0)
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    assert statuses(env.sm) == []
    assert env.fake.enqueued == []
    with env.sm() as s:
        article = s.get(Article, env.article_id)
        assert (article.stock, article.provisional) == (0, 0)
    assert env.engine.status.reachable is False
    assert env.engine.status.last_poll_at is None
    assert "warning.bambuddy_unreachable" in keys(env.sm, env.engine.status)

    env.fake.unavailable = False
    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 1)
    assert env.engine.status.reachable is True


async def test_unclear_blocks_new_dispatch(engine_env):
    env = engine_env
    env.fake.fail_next("enqueue", BambuddyUnavailable("timeout"))
    await env.engine.tick(poll=False)
    assert statuses(env.sm) == ["unclear"]
    assert env.engine.status.reachable is False
    with env.sm.begin() as s:
        make_entry(s, env.article_id, 1, origin="manual")
    env.fake.fail_next("list_all_items", BambuddyUnavailable("timeout"))
    env.fake.calls.clear()

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 0)
    assert "enqueue" not in env.fake.calls
    assert statuses(env.sm) == ["unclear"]
    assert queue_of(env.sm) == [(env.article_id, 1, "manual")]
    assert env.engine.status.unresolved == 1
    assert EngineWarning("warning.unclear_prints", {"count": 1}) in warnings(
        env.sm, env.engine.status
    )


async def test_refused_search_becomes_a_warning(engine_env):
    """Final review 8: Bambuddy refusing the search for an unclear print is shown."""
    env = engine_env
    env.fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)
    env.fake.fail_next("list_all_items", BambuddyRejected("Forbidden", status=403))

    await env.engine.tick(poll=False)

    assert env.engine.status.resolve_error == "403: Forbidden"
    assert env.engine.status.unresolved == 1
    assert EngineWarning("warning.resolve_rejected", {"message": "403: Forbidden"}) in warnings(
        env.sm, env.engine.status
    )

    await env.engine.tick(poll=False)

    assert env.engine.status.resolve_error is None
    assert statuses(env.sm) == ["queued"]
    assert "warning.resolve_rejected" not in keys(env.sm, env.engine.status)


async def test_rejected_waits_for_regular_tick(engine_env):
    env = engine_env
    env.fake.fail_next(
        "enqueue", BambuddyRejected("No active printers for model: A1", status=400)
    )

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 1, 0)
    assert env.engine.status.rejected == "400: No active printers for model: A1"
    assert env.engine.status.rejected_name == "Template A"
    assert statuses(env.sm) == ["returned"]
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    # Final review 7: the warning names the article of the refused print.
    assert EngineWarning(
        "warning.bambuddy_rejected",
        {"name": "Template A", "message": "400: No active printers for model: A1"},
    ) in warnings(env.sm, env.engine.status)

    env.fake.calls.clear()
    result = await env.engine.tick(poll=False)
    assert result == TickResult(None, 0, 0)
    assert "enqueue" not in env.fake.calls

    result = await env.engine.tick(poll=True)
    assert result == TickResult(None, 0, 1)
    assert len(env.fake.enqueued) == 1
    assert env.engine.status.rejected is None
    assert env.engine.status.rejected_name is None


async def test_queue_stopped_unclear_not_resent(engine_env):
    env = engine_env
    d = never_sent(env.sm, env.article_id)
    setting(env.sm, "queue_running", False)

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 0)
    assert env.fake.enqueued == []
    with env.sm() as s:
        assert s.get(Dispatch, d).status == "returned"
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]

    setting(env.sm, "queue_running", True)
    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 1)
    assert len(env.fake.enqueued) == 1


async def test_paused_article_unclear_not_resent(engine_env):
    env = engine_env
    d = never_sent(env.sm, env.article_id)
    with env.sm.begin() as s:
        pause_article(s, env.article_id, "two_failures", "auto")

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 0)
    assert env.fake.enqueued == []
    with env.sm() as s:
        assert s.get(Dispatch, d).status == "returned"
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]


async def test_poll_rejection_blocks_dispatch(engine_env):
    env = engine_env
    await env.engine.tick(poll=False)
    with env.sm.begin() as s:
        make_entry(s, env.article_id, 1, origin="manual")
    env.fake.fail_next("get_queue_item", BambuddyRejected("Forbidden", status=403))
    env.fake.calls.clear()

    result = await env.engine.tick(poll=True)

    assert result == TickResult(None, 0, 0)
    assert "enqueue" not in env.fake.calls
    assert env.engine.status.poll_error == "403: Forbidden"
    assert EngineWarning("warning.poll_rejected", {"message": "403: Forbidden"}) in warnings(
        env.sm, env.engine.status
    )
    # A tick after a booking does not ask again: still nothing handed over.
    result = await env.engine.tick(poll=False)
    assert result == TickResult(None, 0, 0)
    # The next regular tick is answered: the error is gone and the entry goes out.
    result = await env.engine.tick(poll=True)
    assert env.engine.status.poll_error is None
    assert result == TickResult(None, 0, 1)


async def test_poll_refusal_survives_cut_short_poll(engine_env):
    """Decision 15: only a poll that reaches its end clears a refusal (fix round 1)."""
    env = engine_env
    await env.engine.tick(poll=False)
    with env.sm.begin() as s:
        make_entry(s, env.article_id, 1, origin="manual")
    env.fake.fail_next("get_queue_item", BambuddyRejected("Forbidden", status=403))
    await env.engine.tick(poll=True)
    assert env.engine.status.poll_error == "403: Forbidden"

    env.fake.unavailable = True
    await env.engine.tick(poll=True)
    assert env.engine.status.reachable is False
    assert env.engine.status.poll_error == "403: Forbidden"

    env.fake.unavailable = False
    env.fake.calls.clear()
    result = await env.engine.tick(poll=False)

    assert env.engine.status.reachable is True
    assert result == TickResult(None, 0, 0)
    assert "enqueue" not in env.fake.calls
    assert env.engine.status.poll_error == "403: Forbidden"

    result = await env.engine.tick(poll=True)
    assert env.engine.status.poll_error is None
    assert result == TickResult(None, 0, 1)


async def test_refusal_in_a_cut_short_poll_is_kept(engine_env):
    """A refusal found before the poll is cut short still blocks (no complete poll ran)."""
    env = engine_env
    await env.engine.tick(poll=False)
    env.fake.fail_next("list_printers", BambuddyRejected("Forbidden", status=403))
    env.fake.fail_next("list_queue", BambuddyUnavailable("timeout"))

    await env.engine.tick(poll=True)

    assert env.engine.status.reachable is False
    assert env.engine.status.poll_error == "403: Forbidden"


async def ambiguous_hand_over(env, file=1):
    """A print whose answer got lost, plus a second unknown own entry of the same file and
    plate made after the send: the next tick cannot tell them apart (Spec 7.6)."""
    env.fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)
    (real,) = [i for i in await env.fake.list_all_items() if i.library_file_id == file]
    other = env.fake.add_foreign_item(
        "Copy", created_by=env.fake.username, library_file_id=file, plate_id=1
    )
    return [real.id, other.id]


async def test_lost_answer_is_found_by_the_next_tick(engine_env):
    """Spec 7.6: the answer to `enqueue` gets lost; the next tick finds the entry by user,
    file, plate and time, adopts it, and sends nothing a second time."""
    env = engine_env
    env.fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)
    assert statuses(env.sm) == ["unclear"]
    (entry,) = await env.fake.list_all_items()
    env.fake.calls.clear()

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 0)
    assert env.fake.calls.count("enqueue") == 0
    assert len(env.fake.enqueued) == 1
    assert "list_all_items" in env.fake.calls
    assert statuses(env.sm) == ["queued"]
    assert item_ids(env.sm) == [entry.id]
    assert env.engine.status.unresolved == 0
    assert env.engine.status.duplicates == []
    assert len(await env.fake.list_all_items()) == 1


async def test_nothing_new_while_the_own_user_is_unknown(engine_env):
    """Without its Bambuddy user the search decides nothing: the print stays unclear and
    nothing new is handed over (Spec 7.6: until the hand-over is clear, nothing more)."""
    from reprintbuddy.models import Setting

    env = engine_env
    env.fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)
    with env.sm() as s:
        s.merge(Setting(key="bambuddy_username", value="null"))
        s.commit()
    with env.sm.begin() as s:
        make_entry(s, env.article_id, 1, origin="manual")
    env.fake.calls.clear()

    result = await env.engine.tick(poll=False)

    assert result == TickResult(None, 0, 0)
    assert "enqueue" not in env.fake.calls
    assert statuses(env.sm) == ["unclear"]
    assert env.engine.status.unresolved == 1
    assert env.engine.status.reachable is True
    found = keys(env.sm, env.engine.status)
    assert "warning.unclear_prints" in found and "warning.resolve_no_user" in found


async def test_duplicates_kept_after_settling(engine_env):
    env = engine_env
    ids = await ambiguous_hand_over(env)

    await env.engine.tick(poll=False)
    assert env.engine.status.duplicates == [ids]
    assert statuses(env.sm) == ["missing"]
    await env.engine.tick(poll=False)

    assert env.engine.status.duplicates == [ids]
    assert EngineWarning("warning.duplicates", {"ids": [ids]}) in warnings(
        env.sm, env.engine.status
    )
    assert len(env.fake.enqueued) == 1


async def test_duplicates_warning_ends_when_pause_is_lifted(engine_env):
    """Final review 2: the duplicate warning does not stay once the pause is lifted.

    No print holds these ids (none was adopted), so the group remembers its article.
    """
    from reprintbuddy.domain.pauses import lift_pause

    env = engine_env
    ids = await ambiguous_hand_over(env)
    await env.engine.tick(poll=False)
    assert "warning.duplicates" in keys(env.sm, env.engine.status)
    assert env.engine.status.duplicate_articles == {i: env.article_id for i in ids}

    with env.sm.begin() as s:
        lift_pause(s, env.article_id, "user")
    await env.engine.tick(poll=False)

    assert env.engine.status.duplicates == []
    assert env.engine.status.duplicate_articles == {}
    assert "warning.duplicates" not in keys(env.sm, env.engine.status)


async def test_duplicates_of_two_ticks_are_both_kept(engine_env):
    """A later report does not drop a group whose article is still paused."""
    env = engine_env
    env.engine.status.duplicates = [[90, 91]]
    env.engine.status.duplicate_articles = {90: env.article_id, 91: env.article_id}
    with env.sm.begin() as s:
        pause_article(s, env.article_id, "unclear", "auto")
    other = second_article(env.sm)
    ids = await ambiguous_hand_over(env, file=2)

    await env.engine.tick(poll=False)

    assert env.engine.status.duplicates == [[90, 91], ids]
    with env.sm() as s:
        assert s.get(Article, other).pause_reason == "unclear"


async def test_duplicate_group_without_known_article_is_dropped(engine_env):
    env = engine_env
    env.engine.status.duplicates = [[90, 91]]

    await env.engine.tick(poll=False)

    assert env.engine.status.duplicates == []


async def test_poll_notes_whether_the_queue_list_was_read(engine_env):
    """Final review 4: `bambuddy_queue_ok` follows the poll's read of Bambuddy's queue."""
    env = engine_env
    assert env.engine.status.bambuddy_queue_ok is False

    await env.engine.tick(poll=True)
    assert env.engine.status.bambuddy_queue_ok is True

    env.fake.fail_next("list_queue", BambuddyRejected("Forbidden", status=403))
    await env.engine.tick(poll=True)
    assert env.engine.status.bambuddy_queue_ok is False

    await env.engine.tick(poll=True)
    assert env.engine.status.bambuddy_queue_ok is True

    env.fake.fail_next("list_queue", BambuddyUnavailable("timeout"))
    await env.engine.tick(poll=True)
    assert env.engine.status.bambuddy_queue_ok is False

    await env.engine.tick(poll=True)
    env.fake.unavailable = True  # the poll is cut short before the list
    await env.engine.tick(poll=True)
    assert env.engine.status.bambuddy_queue_ok is False


async def test_printer_in_maintenance_waits_without_pause(engine_env):
    env = engine_env
    printer = env.fake.add_printer("Printer 4", "A1", active=False)
    update_article(env.sm, env.article_id, printer_id=printer.id)

    result = await env.engine.tick(poll=True)

    assert result == TickResult(None, 1, 0)
    assert env.fake.enqueued == []
    with env.sm() as s:
        assert s.get(Article, env.article_id).paused is False
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    assert env.engine.status.waiting_for_printer == [env.article_id]
    assert EngineWarning("warning.waiting_for_printer", {"names": ["Template A"]}) in warnings(
        env.sm, env.engine.status
    )

    # A tick after a booking keeps the last answer and still skips it.
    assert await env.engine.tick(poll=False) == TickResult(None, 0, 0)
    assert env.fake.enqueued == []


# ---- no Bambuddy, hooks, warnings ------------------------------------------------------


async def test_no_client_rule_still_runs(engine_env):
    env = engine_env
    engine = Engine(env.sm, lambda: None)

    result = await engine.tick(poll=True)

    assert result == TickResult(None, 1, 0)
    assert env.fake.calls == []
    assert engine.status.configured is False
    assert queue_of(env.sm) == [(env.article_id, 1, "rule")]
    found = keys(env.sm, engine.status)
    assert "warning.bambuddy_not_configured" in found
    assert "warning.bambuddy_unreachable" not in found


async def test_configured_known_before_first_tick(engine_env):
    env = engine_env
    assert env.engine.status.configured is True
    assert Engine(env.sm, lambda: None).status.configured is False

    def broken():
        raise RuntimeError("settings unreadable")

    assert Engine(env.sm, broken).status.configured is False
    assert "warning.bambuddy_not_configured" not in keys(env.sm, env.engine.status)


def test_domain_does_not_import_web():
    """Layering: the domain uses the neutral `reprintbuddy.i18n`, never the web package."""
    from pathlib import Path

    import reprintbuddy
    from reprintbuddy import i18n
    from reprintbuddy.web import i18n as web_i18n

    domain = Path(reprintbuddy.__file__).parent / "domain"
    offenders = [
        p.name for p in domain.rglob("*.py") if "reprintbuddy.web" in p.read_text("utf-8")
    ]
    assert offenders == []
    assert web_i18n.translate is i18n.translate
    assert web_i18n.load_locales is i18n.load_locales


async def test_poll_hook(engine_env):
    env = engine_env
    seen = []

    async def recorder(client, sm, now):
        seen.append((client, sm, now))

    async def broken(client, sm, now):
        raise RuntimeError("scale offline")

    env.engine.add_poll_hook("recorder", recorder)
    env.engine.add_poll_hook("filament", broken)
    assert set(env.engine.hooks) == {"recorder", "filament"}

    result = await env.engine.tick(poll=True)

    assert result == TickResult(None, 1, 1)
    assert len(seen) == 1 and seen[0][0] is env.fake and seen[0][1] is env.sm
    assert set(env.engine.status.hook_errors) == {"filament"}
    assert "scale offline" in env.engine.status.hook_errors["filament"]

    await env.engine.tick(poll=False)
    assert len(seen) == 1

    env.fake.unavailable = True
    await env.engine.tick(poll=True)
    assert len(seen) == 1


async def test_errors_become_warnings(engine_env):
    status = EngineStatus(
        configured=True, reachable=True, error="boom", hook_errors={"filament": "x"}
    )

    found = warnings(engine_env.sm, status)

    assert EngineWarning("warning.tick_error", {"message": "boom"}) in found
    assert EngineWarning("warning.hook_error", {"names": ["filament"]}) in found


async def test_collect_warnings(engine_env):
    env = engine_env
    with env.sm.begin() as s:
        pause_article(s, env.article_id, "two_failures", "auto")
    setting(env.sm, "queue_running", False)
    status = EngineStatus(configured=True, reachable=True, cap_reached=True)

    found = warnings(env.sm, status)

    assert [w.key for w in found] == [
        "warning.queue_stopped",
        "warning.cap_reached",
        "warning.article_paused",
    ]
    assert found[-1] == EngineWarning(
        "warning.article_paused",
        {
            "name": "Template A",
            "reason": translate("en", "pause.reason.two_failures"),
            "article_id": env.article_id,
            "reason_key": "two_failures",
            "has_target": True,
        },
    )


async def test_paused_without_reason_gets_the_general_text(engine_env):
    """Final review 9: a pause without a reason never reads "Name: "."""
    from reprintbuddy.domain.engine import warning_text

    env = engine_env
    update_article(env.sm, env.article_id, paused=True, pause_reason=None)

    found = warnings(env.sm, EngineStatus(configured=True, reachable=True))

    assert found == [
        EngineWarning(
            "warning.article_paused",
            {
                "name": "Template A",
                "reason": translate("en", "status.paused"),
                "article_id": env.article_id,
                "reason_key": None,
                "has_target": True,
            },
        )
    ]
    assert warning_text("en", found[0]) == "Template A: paused"


async def test_paused_notice_tells_whether_a_target_is_set(engine_env):
    """p49: the page offers "lift" only where `lift_pause` would not refuse; the text is unchanged."""
    from reprintbuddy.domain.engine import warning_text

    env = engine_env
    update_article(env.sm, env.article_id, paused=True, pause_reason="no_target",
                   target_model=None, printer_id=None)

    (found,) = warnings(env.sm, EngineStatus(configured=True, reachable=True))

    assert found.params["has_target"] is False and found.params["reason_key"] == "no_target"
    assert found.params["article_id"] == env.article_id
    assert warning_text("en", found) == f"Template A: {translate('en', 'pause.reason.no_target')}"


async def test_collect_warnings_order(engine_env):
    env = engine_env
    with env.sm.begin() as s:
        pause_article(s, env.article_id, "unclear", "auto")
        make_dispatch(s, env.article_id, status="unclear")
    setting(env.sm, "queue_running", False)
    status = EngineStatus(
        configured=False,
        reachable=False,
        poll_error="403: Forbidden",
        unexpected=["paused"],
        waiting_review=[7, 8],
        rejected="400: No",
        rejected_name="Template B",
        resolve_error="403: Nope",
        duplicates=[[1, 2]],
        error="boom",
        hook_errors={"b": "x", "a": "y"},
        skipped_for_update=True,
        cap_reached=True,
        waiting_for_printer=[env.article_id],
    )

    found = warnings(env.sm, status)

    assert found == [
        EngineWarning("warning.bambuddy_not_configured", {}),
        EngineWarning("warning.poll_rejected", {"message": "403: Forbidden"}),
        EngineWarning("warning.unexpected_answer", {"statuses": ["paused"]}),
        EngineWarning("warning.waiting_for_review", {"count": 2}),
        EngineWarning("warning.bambuddy_rejected", {"name": "Template B", "message": "400: No"}),
        EngineWarning("warning.unclear_prints", {"count": 1}),
        EngineWarning("warning.resolve_rejected", {"message": "403: Nope"}),
        EngineWarning("warning.duplicates", {"ids": [[1, 2]]}),
        EngineWarning("warning.tick_error", {"message": "boom"}),
        EngineWarning("warning.hook_error", {"names": ["a", "b"]}),
        EngineWarning("warning.update_running", {}),
        EngineWarning("warning.queue_stopped", {}),
        EngineWarning("warning.cap_reached", {"cap": 10}),
        EngineWarning("warning.waiting_for_printer", {"names": ["Template A"]}),
        EngineWarning(
            "warning.article_paused",
            {
                "name": "Template A",
                "reason": translate("en", "pause.reason.unclear"),
                "article_id": env.article_id,
                "reason_key": "unclear",
                "has_target": True,
            },
        ),
    ]


async def test_warning_texts_exist_and_format(engine_env):
    """Every warning key has an English and a German text that takes its parameters."""
    from reprintbuddy.domain.engine import warning_text

    env = engine_env
    with env.sm.begin() as s:
        pause_article(s, env.article_id, "unclear", "auto")
        make_dispatch(s, env.article_id, status="unclear")
    setting(env.sm, "queue_running", False)
    status = EngineStatus(
        configured=True, reachable=False, poll_error="403: Forbidden", unexpected=["paused"],
        waiting_review=[7], rejected="400: No", rejected_name="Template B",
        resolve_error="403: Nope", duplicates=[[1, 2], [5, 6]], error="boom",
        hook_errors={"a": "x"}, skipped_for_update=True, cap_reached=True,
        waiting_for_printer=[env.article_id],
    )
    found = warnings(env.sm, status)
    assert len(found) == 15
    for lang in ("en", "de"):
        for warning in found:
            text = warning_text(lang, warning)
            assert "{" not in text and "[" not in text, (lang, warning.key, text)
    groups = EngineWarning("warning.duplicates", {"ids": [[1, 2], [5, 6]]})
    assert "(entries 1, 2; 5, 6)" in warning_text("en", groups)
    assert warning_text("de", EngineWarning("warning.cap_reached", {"cap": 3})) == (
        "Obergrenze von 3 offenen Drucken in Bambuddy erreicht; der Rest wartet in Reprintbuddy."
    )
    refused = EngineWarning("warning.bambuddy_rejected", {"name": "Template B", "message": "400: No"})
    for lang in ("en", "de"):
        assert "Template B" in warning_text(lang, refused) and "400: No" in warning_text(lang, refused)
    review = warning_text("en", EngineWarning("warning.waiting_for_review", {"count": 1}))
    assert "Print Without Review" in review and "queue:start_unreviewed" in review


# ---- the loop --------------------------------------------------------------------------


async def test_run_loop_requested_and_regular(engine_env, monkeypatch):
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch)

    async with running(engine, lambda: 0.05):
        await wait_until(lambda: calls == [True])
        engine.request_tick()
        await wait_until(lambda: len(calls) >= 2)
        assert calls[1] is False

        start = time.monotonic()
        before = calls.count(True)
        while time.monotonic() - start < 0.3:
            engine.request_tick()
            await asyncio.sleep(0.01)

    assert calls.count(True) - before >= 2
    assert calls.count(False) >= 5


async def test_run_loop_survives_errors(engine_env, monkeypatch):
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch, fail_first=True)

    async with running(engine, lambda: 60):
        await wait_until(lambda: engine.status.error is not None)
        assert engine.status.error == repr(RuntimeError("boom"))
        engine.request_tick()
        await wait_until(lambda: len(calls) == 2)
        await wait_until(lambda: engine.status.error is None)

    assert calls == [True, False]


async def test_run_loop_survives_poll_seconds_error(engine_env, monkeypatch, caplog):
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch)
    asked = []

    def poll_seconds():
        asked.append(1)
        if len(asked) == 1:
            raise RuntimeError("database is locked")
        return 0.05

    async with running(engine, poll_seconds) as task:
        await wait_until(lambda: calls == [True] and engine.status.error is not None)
        assert engine.status.error == repr(RuntimeError("database is locked"))
        # The first interval fell back to the default: no second poll soon, but the loop lives.
        engine.request_tick()
        await wait_until(lambda: len(calls) == 2)
        await wait_until(lambda: engine.status.error is None)
        assert not task.done()

    assert calls[:2] == [True, False]
    assert "database is locked" in caplog.text


async def test_poll_seconds_error_keeps_last_interval(engine_env, monkeypatch):
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch)
    asked = []

    def poll_seconds():
        asked.append(1)
        if len(asked) == 2:
            raise RuntimeError("database is locked")
        return 0.03

    async with running(engine, poll_seconds) as task:
        await wait_until(lambda: calls.count(True) >= 4)
        assert not task.done()


# ---- Nachtrag: change feed and an early poll -------------------------------------------


async def test_status_change_published(engine_env):
    env = engine_env
    published = []
    engine = Engine(env.sm, lambda: env.fake, publish=published.append)
    await engine.tick(poll=False)
    assert published == [frozenset({"bambuddy"})]
    published.clear()

    env.fake.unavailable = True
    await engine.tick(poll=True)
    assert engine.status.reachable is False
    assert published == [frozenset({"bambuddy"})]

    published.clear()
    await engine.tick(poll=True)
    assert published == []


async def test_publish_error_does_not_break_tick(engine_env, caplog):
    env = engine_env

    def publish(topics):
        raise RuntimeError("broker gone")

    engine = Engine(env.sm, lambda: env.fake, publish=publish)

    result = await engine.tick(poll=False)

    assert result == TickResult(None, 1, 1)
    assert "broker gone" in caplog.text


async def test_request_poll_runs_regular_tick(engine_env, monkeypatch):
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch)

    async with running(engine, lambda: 60):
        await wait_until(lambda: calls == [True])
        engine.request_tick()
        await wait_until(lambda: len(calls) == 2)
        engine.request_poll()
        await wait_until(lambda: len(calls) == 3, timeout=0.1)

    assert calls == [True, False, True]


async def test_several_poll_requests_before_the_loop_wakes_run_one_poll(engine_env, monkeypatch):
    """v0.3.6 p87: a second click on "Query Bambuddy now" while the first is still asked for
    sets the same flag; the loop polls once."""
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch)

    async with running(engine, lambda: 60):
        await wait_until(lambda: calls == [True])
        engine.request_poll()
        engine.request_poll()
        engine.request_poll()
        await wait_until(lambda: len(calls) == 2)
        await asyncio.sleep(0.05)

    assert calls == [True, True]


async def test_poll_requests_during_a_running_poll_add_one_more(engine_env, monkeypatch):
    """v0.3.6 p87: clicks while a poll runs are not lost (the poll may have read before the
    print ended) and not multiplied: exactly one poll follows."""
    engine = engine_env.engine
    calls = []
    release = asyncio.Event()
    running_poll = asyncio.Event()

    async def tick(*, poll):
        calls.append(poll)
        if len(calls) == 2:
            running_poll.set()
            await release.wait()
        return TickResult(None, 0, 0)

    monkeypatch.setattr(engine, "tick", tick)

    async with running(engine, lambda: 60):
        await wait_until(lambda: calls == [True])
        engine.request_poll()
        await wait_until(running_poll.is_set)  # the second poll is under way
        engine.request_poll()
        engine.request_poll()
        release.set()
        await wait_until(lambda: len(calls) == 3)
        await asyncio.sleep(0.05)

    assert calls == [True, True, True]


# ---- p41: right after an update, ask Bambuddy again soon -------------------------------


def record_update_ticks(engine, monkeypatch, *, updates, via_status=False):
    """The first `updates` ticks are skipped for an update, the rest run.

    `via_status`: the tick itself runs but stops its hand-over for the update (the
    result is not `"update"`, only `status.skipped_for_update` tells).
    """
    calls = []

    async def tick(*, poll):
        calls.append(poll)
        skipped = len(calls) <= updates
        engine.status.skipped_for_update = skipped
        if skipped and not via_status:
            return TickResult("update", 0, 0)
        return TickResult(None, 0, 0)

    monkeypatch.setattr(engine, "tick", tick)
    return calls


async def test_tick_skipped_for_update_is_retried_soon(engine_env, monkeypatch):
    """p41: a tick skipped for an update is followed by a regular tick after a few
    seconds, not only after `poll_minutes`, until the update is over."""
    import reprintbuddy.domain.engine as engine_module

    monkeypatch.setattr(engine_module, "UPDATE_RETRY_SECONDS", 0.02)
    engine = engine_env.engine
    calls = record_update_ticks(engine, monkeypatch, updates=2)

    async with running(engine, lambda: 60):
        await wait_until(lambda: len(calls) >= 3)
        await asyncio.sleep(0.1)  # once the update is over, the interval holds again

    assert calls == [True, True, True]
    assert engine.status.skipped_for_update is False


async def test_hand_over_stopped_for_update_is_retried_soon(engine_env, monkeypatch):
    """Sister path: the hand-over stopped for an update in the middle of a tick."""
    import reprintbuddy.domain.engine as engine_module

    monkeypatch.setattr(engine_module, "UPDATE_RETRY_SECONDS", 0.02)
    engine = engine_env.engine
    calls = record_update_ticks(engine, monkeypatch, updates=1, via_status=True)

    async with running(engine, lambda: 60):
        await wait_until(lambda: len(calls) >= 2)
        await asyncio.sleep(0.1)

    assert calls == [True, True]


async def test_skipped_short_tick_brings_the_poll_forward(engine_env, monkeypatch):
    """A booking's short tick skipped for an update also leads to a regular tick soon."""
    import reprintbuddy.domain.engine as engine_module

    monkeypatch.setattr(engine_module, "UPDATE_RETRY_SECONDS", 0.02)
    engine = engine_env.engine
    calls = []

    async def tick(*, poll):
        calls.append(poll)
        skipped = len(calls) == 2  # the first poll ran; the booking's tick hits the update
        engine.status.skipped_for_update = skipped
        return TickResult("update" if skipped else None, 0, 0)

    monkeypatch.setattr(engine, "tick", tick)

    async with running(engine, lambda: 60):
        await wait_until(lambda: calls == [True])
        engine.request_tick()
        await wait_until(lambda: len(calls) >= 3)
        await asyncio.sleep(0.1)

    assert calls == [True, False, True]


def test_unknown_own_user_has_its_own_translated_warning(engine_env):
    """p50: without the own Bambuddy user the search cannot run; the warning says so in
    the interface language instead of quoting an English text."""
    from reprintbuddy.domain import dispatch

    status = EngineStatus(configured=True, reachable=True, resolve_error=dispatch.NO_OWN_USER)
    found = warnings(engine_env.sm, status)
    assert EngineWarning("warning.resolve_no_user", {}) in found
    assert all(w.key != "warning.resolve_rejected" for w in found)
    for lang in ("de", "en"):
        text = translate(lang, "warning.resolve_no_user")
        assert text != "warning.resolve_no_user" and "{" not in text


# ---- a wait that times out early is still the due poll ---------------------------------


def time_out_early(engine, monkeypatch):
    """The wait ends by timeout well before the deadline, as asyncio may do on Windows
    (`loop._clock_resolution` 0.015625 s): a timeout within the clock resolution counts
    as reached. Halved here, so the test does not depend on the clock."""

    async def wait(timeout):
        try:
            await asyncio.wait_for(engine._wake.wait(), timeout=timeout / 2)
        except TimeoutError:
            return False
        return True

    monkeypatch.setattr(engine, "_wait_for_wake", wait)


async def test_early_timeout_still_runs_the_due_poll(engine_env, monkeypatch):
    """The wait timed out, so the poll is due: a regular tick, never a short one."""
    engine = engine_env.engine
    calls = record_ticks(engine, monkeypatch)
    time_out_early(engine, monkeypatch)

    async with running(engine, lambda: 0.2):
        await wait_until(lambda: len(calls) >= 3)

    assert calls[:3] == [True, True, True]


async def test_early_timeout_after_update_skip_still_polls(engine_env, monkeypatch):
    """p41 with an early timeout: the retry after an update is a regular tick."""
    import reprintbuddy.domain.engine as engine_module

    monkeypatch.setattr(engine_module, "UPDATE_RETRY_SECONDS", 0.1)
    engine = engine_env.engine
    calls = record_update_ticks(engine, monkeypatch, updates=1)
    time_out_early(engine, monkeypatch)

    async with running(engine, lambda: 60):
        await wait_until(lambda: len(calls) >= 2)
        await asyncio.sleep(0.15)

    assert calls == [True, True]


def test_hold_lets_a_waiting_tick_give_up(engine_env):
    """A restore holds the engine (Spec 11.6): a tick that waited for it does nothing."""
    env = engine_env
    first_check_done = threading.Event()
    real_check = env.engine.update_running

    def noting_check():
        answer = real_check()
        first_check_done.set()
        return answer

    env.engine.update_running = noting_check

    async def scenario():
        async with env.engine.hold():
            assert env.engine.held()
            task = asyncio.create_task(env.engine.tick(poll=False))
            # The tick asks (in a worker thread, p96) whether an update runs, then waits for the lock.
            assert await asyncio.to_thread(first_check_done.wait, 5)
            await asyncio.sleep(0.05)
            env.updating = True  # the restore says "do not touch the database"
        assert not env.engine.held()
        return await task

    result = asyncio.run(scenario())
    assert result.skipped == "update"
    assert result.created == 0
    with env.sm() as s:
        assert s.scalars(select(Dispatch)).all() == []


# ---- p96 (v0.3.8): the question whether an update runs does not hold the event loop --------------------------


async def _loop_stays_free(task, release: threading.Event, *, steps: int = 20):
    """While `task` waits for the blocked question, the loop still turns (a page would be answered)."""
    for _ in range(steps):
        await asyncio.sleep(0.01)
    assert not task.done()
    release.set()


async def test_p96_a_blocked_update_question_does_not_hold_the_event_loop(engine_env):
    env = engine_env
    release = threading.Event()
    asked_from: list[str] = []

    def blocked_question():
        try:
            asyncio.get_running_loop()
            asked_from.append("event loop")
        except RuntimeError:
            asked_from.append("worker thread")
        release.wait(5)  # systemd taking its time
        return False

    env.engine.update_running = blocked_question
    task = asyncio.create_task(env.engine.tick(poll=False))
    await _loop_stays_free(task, release)

    result = await asyncio.wait_for(task, 10)
    assert result == TickResult(None, 1, 1)  # the decision is the one it always was
    assert set(asked_from) == {"worker thread"}


async def test_p96_the_question_before_the_send_is_also_asked_off_the_loop(engine_env):
    env = engine_env
    release = threading.Event()
    calls = []

    def question():
        calls.append(1)
        if len(calls) == 3:  # the third is the one right before the send (`may_send`)
            release.wait(5)
        return False

    env.engine.update_running = question
    task = asyncio.create_task(env.engine.tick(poll=False))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(calls) == 3:
            break
    await _loop_stays_free(task, release)
    result = await asyncio.wait_for(task, 10)
    assert result == TickResult(None, 1, 1)
    assert len(env.fake.enqueued) == 1


async def test_p96_an_update_found_by_the_threaded_question_still_stops_the_send(engine_env):
    env = engine_env
    answers = iter([False, False, True])  # tick start, hand-over loop, right before the send

    env.engine.update_running = lambda: next(answers, True)
    result = await env.engine.tick(poll=False)
    assert result == TickResult(None, 1, 0)
    assert env.fake.enqueued == []
    assert statuses(env.sm) == ["returned"]
    assert env.engine.status.skipped_for_update is True
