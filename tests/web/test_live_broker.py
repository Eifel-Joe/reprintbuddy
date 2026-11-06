"""In-memory broker for change notices (thread-safe publish, asyncio subscriptions)."""

import asyncio
import threading

import pytest

from reprintbuddy.web.live import BrokerClosed, LiveBroker


async def test_publish_reaches_every_subscriber():
    broker = LiveBroker()
    a, b = broker.subscribe(), broker.subscribe()
    broker.publish({"journal"})
    assert await a.wait(1) == frozenset({"journal"})
    assert await b.wait(1) == frozenset({"journal"})


async def test_quick_publishes_arrive_together():
    broker = LiveBroker()
    sub = broker.subscribe()
    broker.publish({"part"})
    broker.publish({"order:1"})
    assert await sub.wait(1) == frozenset({"part", "order:1"})
    assert await sub.wait(0.05) is None


def test_state_counts_and_differs_per_broker():
    one, two = LiveBroker(), LiveBroker()
    assert one.state.endswith(":0")
    one.publish([])
    assert one.state.endswith(":0")
    one.publish({"journal"})
    assert one.state.endswith(":1")
    assert len(one.state.split(":")[0]) == 8
    assert one.state.split(":")[0] != two.state.split(":")[0]


async def test_publish_from_another_thread_arrives():
    broker = LiveBroker()
    sub = broker.subscribe()
    t = threading.Thread(target=broker.publish, args=({"part"},))
    t.start()
    t.join()
    assert await sub.wait(1) == frozenset({"part"})


async def test_close_ends_open_and_later_subscriptions():
    broker = LiveBroker()
    sub = broker.subscribe()
    waiter = asyncio.create_task(sub.wait(5))
    await asyncio.sleep(0.05)
    broker.close()
    broker.close()
    with pytest.raises(BrokerClosed):
        await waiter
    with pytest.raises(BrokerClosed):
        await broker.subscribe().wait(1)


async def test_closing_a_subscription_unsubscribes():
    broker = LiveBroker()
    sub = broker.subscribe()
    assert broker.subscriber_count == 1
    sub.close()
    sub.close()
    assert broker.subscriber_count == 0


def test_a_subscriber_on_a_closed_loop_is_dropped():
    broker = LiveBroker()

    async def make():
        broker.subscribe()

    t = threading.Thread(target=lambda: asyncio.run(make()))
    t.start()
    t.join()
    assert broker.subscriber_count == 1
    broker.publish({"journal"})
    assert broker.subscriber_count == 0
