"""The engine deletes the prints a cancel noted for Bambuddy (Spec 7.2a, 7.6, 7.7; 0.4.1 p382).

Only waiting rule prints, the newest first, at most what is still over the target on that run; never
a pause. Bambuddy unreachable: the note stays and a later run catches up. A print that started in
between (400) stays and is followed as printing. A refusal (403) is not tried again. A delete whose
answer got lost is settled by asking Bambuddy, never taken for a delete by somebody else.
"""

from datetime import date

from sqlalchemy import select

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.models import Article, Dispatch, JournalEntry, Withdrawal


def _order(sm, article_id: int, qty: int, reference: str = "R-1") -> int:
    with sm.begin() as s:
        return orders.create_order(
            s, reference=reference, order_date=date(2026, 11, 11), items=[ArticleItem(article_id, qty)], actor="user"
        ).id


def _cancel(sm, order_id: int):
    with sm.begin() as s:
        return orders.cancel_order(s, order_id, "user")


def _statuses(sm) -> list[str]:
    with sm() as s:
        return list(s.scalars(select(Dispatch.status).order_by(Dispatch.id)))


def _items(sm) -> list[int]:
    with sm() as s:
        return list(s.scalars(select(Dispatch.bambuddy_item_id).order_by(Dispatch.id)))


def _note(sm):
    with sm() as s:
        row = s.get(Withdrawal, 1)
        return None if row is None else (row.prints, row.refused, row.unreached)


def _journal(sm, kind):
    with sm() as s:
        return list(s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id)))


def _paused(sm, article_id) -> tuple[bool, str | None]:
    with sm() as s:
        article = s.get(Article, article_id)
        return article.paused, article.pause_reason


async def _three_handed_over(env) -> int:
    """Stock 1, minimum 1, buffer 0; an order of 3 plans three prints, all handed over and waiting."""
    with env.sm.begin() as s:
        s.get(Article, env.article_id).stock = 1
    order_id = _order(env.sm, env.article_id, 3)
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["queued"] * 3
    return order_id


async def test_the_short_tick_after_a_cancel_deletes_them(engine_env):  # A1: no wait for the interval
    env = engine_env
    order_id = await _three_handed_over(env)
    items = _items(env.sm)
    assert _cancel(env.sm, order_id).bambuddy_prints == 3

    await env.engine.tick(poll=False)

    assert _statuses(env.sm) == ["withdrawn"] * 3
    assert sorted(env.fake.deleted) == sorted(items) and env.fake.deleted[0] == items[-1]  # newest first
    assert await env.fake.list_queue() == []
    assert _paused(env.sm, env.article_id) == (False, None)
    (line,) = _journal(env.sm, "print_withdrawn")
    assert (line.actor, line.qty, line.article_id, line.order_id) == ("auto", 3, env.article_id, order_id)
    assert sorted(line.details["item_ids"]) == sorted(items)
    assert _note(env.sm) is None
    await env.engine.tick(poll=True)  # the rule plans nothing again, nothing is handed over
    assert len(env.fake.enqueued) == 3 and _statuses(env.sm) == ["withdrawn"] * 3


async def test_unreachable_keeps_the_note_and_a_later_run_catches_up(engine_env):
    env = engine_env
    order_id = await _three_handed_over(env)
    env.fake.unavailable = True
    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["queued"] * 3
    assert _note(env.sm) == (3, None, True)  # A2: a run could not do it

    env.fake.unavailable = False
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn"] * 3 and _note(env.sm) is None


async def test_a_new_order_before_the_run_takes_back_fewer(engine_env):  # review focus 3
    env = engine_env
    order_id = await _three_handed_over(env)
    env.fake.unavailable = True
    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)
    _order(env.sm, env.article_id, 2, reference="R-2")  # needs two of the three again
    env.fake.unavailable = False
    await env.engine.tick(poll=False)
    assert sorted(_statuses(env.sm)) == ["queued", "queued", "withdrawn"]
    assert _note(env.sm) is None


async def test_a_lost_answer_after_the_delete_is_settled_without_a_pause(engine_env):  # review focus 2
    env = engine_env
    order_id = await _three_handed_over(env)
    _cancel(env.sm, order_id)
    env.fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)
    with env.sm() as s:
        marked = s.scalars(select(Dispatch).where(Dispatch.withdraw_sent_at.is_not(None))).all()
        assert [d.status for d in marked] == ["queued"]
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn"] * 3
    assert _paused(env.sm, env.article_id) == (False, None)
    assert _journal(env.sm, "print_missing") == []


async def test_the_poll_settles_a_lost_delete_as_withdrawn(engine_env):
    env = engine_env
    order_id = await _three_handed_over(env)
    _cancel(env.sm, order_id)
    env.fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)
    await env.engine.tick(poll=True)  # the poll asks first: the marked print is gone
    assert _statuses(env.sm) == ["withdrawn"] * 3
    assert _paused(env.sm, env.article_id) == (False, None)


async def test_a_failure_before_the_delete_tries_again(engine_env):
    env = engine_env
    order_id = await _three_handed_over(env)
    _cancel(env.sm, order_id)
    env.fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"))
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["queued"] * 3
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn"] * 3
    assert _paused(env.sm, env.article_id) == (False, None)


async def test_a_print_that_started_meanwhile_stays_and_is_not_tried_again(engine_env):  # review focus 1
    env = engine_env
    order_id = await _three_handed_over(env)
    newest = _items(env.sm)[-1]
    _cancel(env.sm, order_id)
    env.fake.set_status(newest, "printing")  # Reprintbuddy still has it as waiting
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn", "withdrawn", "printing"]
    assert _paused(env.sm, env.article_id) == (False, None)
    calls = env.fake.calls.count("delete_queue_item")
    await env.engine.tick(poll=False)
    assert env.fake.calls.count("delete_queue_item") == calls  # no try on a printing one
    assert _note(env.sm) == (1, None, False)  # still over the target: the note stays

    env.fake.set_status(newest, "pending")  # the printer did not take the start: waiting again
    await env.engine.tick(poll=True)
    assert _statuses(env.sm) == ["withdrawn"] * 3 and _note(env.sm) is None


async def test_a_note_without_any_open_rule_print_is_dropped(engine_env):
    env = engine_env
    order_id = await _three_handed_over(env)
    newest = _items(env.sm)[-1]
    _cancel(env.sm, order_id)
    env.fake.set_status(newest, "printing")
    await env.engine.tick(poll=False)
    env.fake.set_status(newest, "completed")
    await env.engine.tick(poll=True)
    assert _statuses(env.sm)[-1] == "completed" and _note(env.sm) is None


async def test_without_the_right_it_is_refused_once(engine_env):
    env = engine_env
    order_id = await _three_handed_over(env)
    env.fake.can_delete_own = False
    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["queued"] * 3
    prints, refused, _ = _note(env.sm)
    assert prints == 3 and "queue:delete_own" in refused
    (line,) = _journal(env.sm, "print_withdraw_refused")
    assert (line.actor, line.qty, line.order_id) == ("auto", 3, order_id) and "queue:delete_own" in line.details["message"]
    env.fake.can_delete_own = True
    await env.engine.tick(poll=False)
    assert env.fake.calls.count("delete_queue_item") == 1 and _statuses(env.sm) == ["queued"] * 3
    assert _paused(env.sm, env.article_id) == (False, None)


async def test_a_delete_by_somebody_else_still_pauses(engine_env):  # Spec 7.6 unchanged
    env = engine_env
    await _three_handed_over(env)
    env.fake.delete_item(_items(env.sm)[0])
    await env.engine.tick(poll=True)
    assert _paused(env.sm, env.article_id) == (True, "missing_in_bambuddy")


async def test_nothing_is_deleted_while_an_update_runs(engine_env):
    env = engine_env
    order_id = await _three_handed_over(env)
    _cancel(env.sm, order_id)
    env.updating = True
    await env.engine.tick(poll=False)
    assert env.fake.deleted == [] and _note(env.sm)[0] == 3
    env.updating = False
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn"] * 3


# ---- review of 0.4.1 C (Opus): prints whose state changed in Bambuddy since the last poll ---------


async def test_only_prints_bambuddy_still_keeps_waiting_are_deleted(engine_env):  # review 1
    """The short tick after a cancel works from Reprintbuddy's stored state, up to a poll interval
    old. Bambuddy deletes a finished entry too, so each print is read right before its delete."""
    env = engine_env
    order_id = await _three_handed_over(env)
    oldest = _items(env.sm)[0]
    env.fake.set_status(oldest, "completed")  # finished in Bambuddy, still waiting in Reprintbuddy
    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["completed", "withdrawn", "withdrawn"]
    assert oldest not in env.fake.deleted and len(env.fake.deleted) == 2
    with env.sm() as s:
        assert s.get(Article, env.article_id).provisional == 1  # its pieces wait for the inspection
    assert _note(env.sm) is None


async def test_a_failed_print_is_replaced_not_withdrawn(engine_env):  # review 1, Spec 7.5
    env = engine_env
    order_id = await _three_handed_over(env)
    oldest = _items(env.sm)[0]
    env.fake.set_status(oldest, "failed")
    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)
    assert _statuses(env.sm)[0] == "failed" and oldest not in env.fake.deleted
    assert _journal(env.sm, "print_failed")


async def test_a_print_cancelled_or_deleted_by_somebody_else_still_pauses(engine_env):  # review 1, Spec 7.6
    env = engine_env
    order_id = await _three_handed_over(env)
    oldest = _items(env.sm)[0]
    env.fake.delete_item(oldest)  # deleted in Bambuddy before the cancel, not yet polled
    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)
    assert _statuses(env.sm)[0] == "missing"
    assert _paused(env.sm, env.article_id) == (True, "missing_in_bambuddy")


async def test_a_started_print_gets_a_journal_line(engine_env):  # review 2, Spec 7.2a
    env = engine_env
    order_id = await _three_handed_over(env)
    newest = _items(env.sm)[-1]
    _cancel(env.sm, order_id)
    original = env.fake.delete_queue_item

    async def starts_first(item_id):  # read as waiting, started before the delete arrives: the race
        if item_id == newest:
            env.fake.set_status(item_id, "printing")
        return await original(item_id)

    env.fake.delete_queue_item = starts_first
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn", "withdrawn", "printing"]
    (line,) = _journal(env.sm, "print_withdraw_refused")
    assert line.details["status"] == 400 and "currently printing" in line.details["message"]
    assert line.qty == 1 and line.details["item_id"] == newest
    assert _note(env.sm)[1] is None  # not a refusal for good


async def test_a_400_while_still_waiting_is_tried_again(engine_env):  # review 3
    env = engine_env
    order_id = await _three_handed_over(env)
    _cancel(env.sm, order_id)
    env.fake.fail_next(
        "delete_queue_item", BambuddyRejected("Cannot delete item that is currently printing", status=400)
    )
    await env.engine.tick(poll=False)  # the newest: 400, read again: waiting (the start was not taken)
    prints, refused, _ = _note(env.sm)
    assert refused is None and prints == 1
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn"] * 3 and _note(env.sm) is None


async def test_a_second_cancel_counts_a_print_whose_delete_is_unsettled(engine_env):  # review 4
    env = engine_env
    first = await _three_handed_over(env)
    _cancel(env.sm, first)
    env.fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"), after_effect=True)
    await env.engine.tick(poll=False)  # the newest is deleted, its answer lost: marked
    second = _order(env.sm, env.article_id, 1, reference="R-2")
    assert _cancel(env.sm, second).bambuddy_prints == 3  # the marked one counts in the note
    await env.engine.tick(poll=False)
    assert _statuses(env.sm) == ["withdrawn"] * 3 and _note(env.sm) is None


async def test_a_note_with_nothing_left_to_take_back_warns_not(engine_env):  # review 6
    from reprintbuddy.domain.engine import collect_warnings

    env = engine_env
    order_id = await _three_handed_over(env)
    newest = _items(env.sm)[-1]
    _cancel(env.sm, order_id)
    env.fake.set_status(newest, "printing")
    await env.engine.tick(poll=False)  # two withdrawn, the printing one stays, the note too
    env.fake.unavailable = True
    await env.engine.tick(poll=False)
    with env.sm() as s:
        keys = [w.key for w in collect_warnings(s, env.engine.status)]
    assert "warning.withdraw_waiting" not in keys


async def test_the_poll_takes_the_mark_off_a_print_that_waits_or_prints(engine_env):  # review 9
    env = engine_env
    order_id = await _three_handed_over(env)
    newest = _items(env.sm)[-1]
    _cancel(env.sm, order_id)
    env.fake.fail_next("delete_queue_item", BambuddyUnavailable("timeout"))  # never reached Bambuddy
    env.updating = False
    await env.engine.tick(poll=False)
    env.fake.set_status(newest, "printing")
    with env.sm.begin() as s:
        from reprintbuddy.domain import tracking

        row = s.scalars(select(Dispatch).where(Dispatch.bambuddy_item_id == newest)).one()
        assert row.withdraw_sent_at is not None
        tracking.apply_outcome(s, row.id, "printing", "printing")
        assert row.withdraw_sent_at is None
