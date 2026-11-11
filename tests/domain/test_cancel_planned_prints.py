"""What cancelling an order does to the prints planned because of it (p382).

Prints are not tied to an order: rule 7.1 plans them from the coverage, and an order lowers the
coverage through "available". In 0.4.0 cancelling only released the reservation; since 0.4.1 (Spec
7.2a) it takes back what is then planned over the target, end to end through the engine.
"""

from datetime import date

from sqlalchemy import select

from reprintbuddy import settings as st
from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry


def _order(sm, article_id: int, qty: int) -> int:
    with sm.begin() as s:
        return orders.create_order(
            s, reference="Order 1001", order_date=date(2026, 1, 15),
            items=[ArticleItem(article_id, qty)], actor="user",
        ).id


def _cancel(sm, order_id: int) -> None:
    with sm.begin() as s:
        orders.cancel_order(s, order_id, "user")


def _queue(sm) -> list[tuple[str, int]]:
    with sm() as s:
        rows = s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position))
        return [(r.origin, r.prints) for r in rows]


def _dispatches(sm) -> list[str]:
    with sm() as s:
        return list(s.scalars(select(Dispatch.status).order_by(Dispatch.id)))


async def test_cancel_leaves_handed_over_prints_in_bambuddy(engine_env):
    env = engine_env
    with env.sm.begin() as s:
        s.get(Article, env.article_id).stock = 1  # minimum 1, buffer 0: covered
    order_id = _order(env.sm, env.article_id, 3)  # available -2: three prints
    await env.engine.tick(poll=False)
    assert len(env.fake.enqueued) == 3 and _dispatches(env.sm) == ["queued"] * 3

    _cancel(env.sm, order_id)
    await env.engine.tick(poll=True)

    # 0.4.0: all three stay in Bambuddy and keep counting "in progress"; nothing is withdrawn.
    assert [i.status for i in await env.fake.list_queue()] == ["pending"] * 3
    assert _dispatches(env.sm) == ["queued"] * 3
    assert len(env.fake.enqueued) == 3
    with env.sm() as s:
        art = s.get(Article, env.article_id)
        assert (art.reserved, art.paused) == (0, False)


async def test_cancel_takes_entries_out_of_reprintbuddys_queue(engine_env):
    env = engine_env
    with env.sm() as s:
        st.set_setting(s, "queue_running", False)  # nothing is handed over
    with env.sm.begin() as s:
        s.get(Article, env.article_id).stock = 1
    order_id = _order(env.sm, env.article_id, 3)
    await env.engine.tick(poll=False)
    assert _queue(env.sm) == [("rule", 3)]

    _cancel(env.sm, order_id)
    await env.engine.tick(poll=False)

    # Since 0.4.1 the rule entry is gone with the demand (0.4.0 kept its three prints) ...
    assert _queue(env.sm) == []
    with env.sm() as s:
        st.set_setting(s, "queue_running", True)
    await env.engine.tick(poll=False)
    # ... so nothing is handed over once the queue runs again (0.4.0 handed over all three).
    assert env.fake.enqueued == [] and _queue(env.sm) == []
    with env.sm() as s:
        assert s.get(Article, env.article_id).paused is False
