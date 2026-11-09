"""Sending after the tick (Spec 10.6 "Senden und Fehler", plan v0.3.12 B D3 and the three conditions
of its approval: one sending task at a time, the event loop kept short, "unclear" counts as failed).

Home Assistant is a respx route on a `.test` host; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from factories import make_article
from sqlalchemy import select

from reprintbuddy import settings as st
from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.models import Article, NotifyMark
from reprintbuddy.notify import events as ev
from reprintbuddy.notify.service import TOPIC, Notifier

URL = "http://ha.test:8123"
CALL = f"{URL}/api/services/notify/mobile_app_phone"
PING = f"{URL}/api/"
TOKEN = "eyJ0eXAiOiJKV1QifQ.TOKENCHECK.sig"
NOW = datetime(2026, 11, 9, 12, 0)
_TLS = httpx.create_ssl_context()


def factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=_TLS)


class Env:
    def __init__(self, db) -> None:
        self.db = db
        self.status = EngineStatus(configured=True, reachable=True)
        self.updating = False
        self.published: list[frozenset[str]] = []
        self.notifier = Notifier(
            db,
            http_factory=factory,
            engine_status=lambda: self.status,
            rights=lambda: ev.Rights("ok", ()),
            update_started=lambda: self.updating,
            base=lambda session: "http://192.0.2.7:8080",
            publish=self.published.append,
            now=lambda: NOW,
        )

    async def settle(self) -> None:
        """Wait for the sending task (and any follow-up round) to end."""
        for _ in range(200):
            task = self.notifier.task
            if task is None or task.done():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("the sending task did not end")

    def pause(self, name: str = "Bracket") -> int:
        with self.db.begin() as s:
            a = make_article(s, name=name)
            a.paused, a.pause_reason = True, "two_failures"
            return a.id

    def marks(self) -> list[tuple[str, str]]:
        with self.db() as s:
            return [(m.kind, m.state) for m in s.scalars(select(NotifyMark).order_by(NotifyMark.id))]


@pytest.fixture
def env(db):
    with db.begin() as s:
        st.set_setting(s, "ha_enabled", True, commit=False)
        st.set_setting(s, "ha_url", URL, commit=False)
        st.set_setting(s, "ha_token", TOKEN, commit=False)
        st.set_setting(s, "ha_service", "notify.mobile_app_phone", commit=False)
        st.set_setting(s, "ha_since", datetime(2026, 11, 9, 8, 0, tzinfo=UTC).isoformat(), commit=False)
    return Env(db)


async def test_switched_off_nothing_is_sent_and_nothing_is_followed(db, respx_mock):
    env = Env(db)  # no settings: off
    env.pause()
    env.notifier.after_tick(True)
    await env.settle()
    assert respx_mock.calls.call_count == 0
    assert env.notifier.task is None
    assert env.marks() == []


async def test_a_state_is_sent_once_in_a_task_after_the_tick(env, respx_mock):
    route = respx_mock.post(CALL).respond(200, json=[])
    env.pause()
    env.notifier.after_tick(False)
    assert route.call_count == 0  # not in the tick: in a task of its own
    await env.settle()
    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["title"] == "Article paused"
    assert env.marks() == [("paused", "sent")]
    assert env.notifier.status.last_outcome == "accepted"
    assert env.notifier.status.last_at == NOW
    env.notifier.after_tick(False)
    await env.settle()
    assert route.call_count == 1


async def test_one_sending_task_at_a_time_and_nothing_twice(env, respx_mock):
    """Condition 1 of the plan's approval: two ticks during a hanging message make one request;
    once it is done, nothing goes out twice, and what came up meanwhile follows in one round."""
    release = asyncio.Event()
    calls: list[dict] = []

    async def hanging(request):
        calls.append(json.loads(request.content))
        await release.wait()
        return httpx.Response(200, json=[])

    respx_mock.post(CALL).mock(side_effect=hanging)
    env.pause("Bracket")
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    first = env.notifier.task
    env.notifier.after_tick(False)
    env.notifier.after_tick(True)
    await asyncio.sleep(0.05)
    assert env.notifier.task is first
    assert len(calls) == 1
    env.pause("Cover")  # comes up while the first message hangs
    env.notifier.after_tick(False)
    release.set()
    await env.settle()
    assert [c["message"] for c in calls] == [
        "The automation paused 1 article: Bracket (two failures in a row)",
        "The automation paused 1 article: Cover (two failures in a row)",
    ]
    env.notifier.after_tick(False)
    await env.settle()
    assert len(calls) == 2


async def test_a_hanging_home_assistant_does_not_hold_up_the_engine(engine_env, respx_mock):
    from reprintbuddy.domain.engine import Engine  # noqa: F401  (the fixture builds one)

    env = engine_env
    with env.sm.begin() as s:
        st.set_setting(s, "ha_enabled", True, commit=False)
        st.set_setting(s, "ha_url", URL, commit=False)
        st.set_setting(s, "ha_token", TOKEN, commit=False)
        st.set_setting(s, "ha_service", "notify.mobile_app_phone", commit=False)
        a = s.get(Article, env.article_id)
        a.paused, a.pause_reason = True, "two_failures"
    release = asyncio.Event()

    async def hanging(request):
        await release.wait()
        return httpx.Response(200, json=[])

    respx_mock.post(CALL).mock(side_effect=hanging)
    notifier = Notifier(
        env.sm, http_factory=factory, engine_status=lambda: env.engine.status,
        rights=lambda: ev.Rights("unknown", ()), update_started=lambda: False, base=lambda s: None,
    )
    env.engine.after_tick.append(notifier.after_tick)
    await env.engine.tick(poll=False)
    await asyncio.sleep(0.05)
    assert notifier.task is not None and not notifier.task.done()  # the message hangs
    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(env.engine.tick(poll=False), timeout=2)
    assert asyncio.get_running_loop().time() - started < 2
    assert not env.engine.held()
    release.set()
    await notifier.stop()


@pytest.mark.parametrize("outcome_status", [503, 401])
async def test_a_failed_state_shows_the_warning_and_is_sent_again(env, respx_mock, outcome_status):
    route = respx_mock.post(CALL)
    route.side_effect = [httpx.Response(outcome_status), httpx.Response(200, json=[])]
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem in ("server", "token")
    assert frozenset({TOPIC}) in env.published
    assert env.marks() == [("paused", "due")]
    env.notifier.after_tick(False)
    await env.settle()
    assert route.call_count == 2
    assert env.notifier.status.problem is None
    assert env.marks() == [("paused", "sent")]


async def test_an_unclear_answer_counts_as_failed_for_a_state(env, respx_mock):
    """Condition 3: the state goes out again and may arrive twice (Spec 10.6 "Senden und Fehler")."""
    route = respx_mock.post(CALL)
    route.side_effect = [httpx.ReadTimeout("slow"), httpx.Response(200, json=[])]
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem == "unclear"
    env.notifier.after_tick(False)
    await env.settle()
    assert route.call_count == 2


async def test_the_warning_goes_when_the_check_succeeds_at_a_regular_query(env, respx_mock):
    respx_mock.post(CALL).respond(503)
    ping = respx_mock.get(PING).respond(200, json={"message": "API running."})
    with env.db.begin() as s:
        st.set_setting(s, "ha_events", ["print_done"], commit=False)  # nothing due afterwards
        a = make_article(s)
        s.add(NotifyMark(kind="print_done", ref="99", state="due", first_seen_at=NOW))
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem == "server"
    env.notifier.after_tick(False)  # a short tick: no check
    await env.settle()
    assert ping.call_count == 0
    env.notifier.after_tick(True)
    await env.settle()
    assert ping.call_count == 1
    assert env.notifier.status.problem is None


async def test_switching_off_clears_the_warning(env, respx_mock):
    respx_mock.post(CALL).respond(503)
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem == "server"
    with env.db.begin() as s:
        st.set_setting(s, "ha_enabled", False, commit=False)
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem is None
    assert respx_mock.calls.call_count == 1


async def test_nothing_happens_while_an_update_or_restore_begins(env, respx_mock):
    route = respx_mock.post(CALL).respond(200, json=[])
    env.pause()
    env.updating = True
    env.notifier.after_tick(False)
    await env.settle()
    assert route.call_count == 0
    assert env.marks() == []


async def test_an_update_starting_during_the_send_writes_no_result(env, respx_mock):
    async def starts_update(request):
        env.updating = True
        return httpx.Response(200, json=[])

    respx_mock.post(CALL).mock(side_effect=starts_update)
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert env.marks() == [("paused", "due")]  # the database may be replaced: not touched


async def test_stop_ends_a_hanging_task(env, respx_mock):
    async def hanging(request):
        await asyncio.sleep(60)
        return httpx.Response(200)

    respx_mock.post(CALL).mock(side_effect=hanging)
    env.pause()
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    task = env.notifier.task
    await env.notifier.stop()
    assert task.done()
    env.notifier.after_tick(False)  # stopped: no new task
    assert env.notifier.task is task


async def test_the_spool_list_counts_as_read_only_when_a_regular_query_reached_its_end(env, respx_mock):
    respx_mock.post(CALL).respond(200, json=[])
    seen: list[bool] = []
    original = ev.detect

    def spy(session, ctx):
        seen.append(ctx.filament_known)
        return original(session, ctx)

    env.notifier._detect = spy  # type: ignore[attr-defined]
    env.status.last_poll_at = datetime(2026, 11, 9, 11, 0)
    env.notifier.after_tick(True)  # a new query time: read
    env.notifier.after_tick(True)  # the same time again: the query did not reach its end
    env.notifier.after_tick(False)  # a short tick reads nothing
    env.status.last_poll_at = datetime(2026, 11, 9, 11, 15)
    env.status.hook_errors = {"filament": "boom"}
    env.notifier.after_tick(True)  # the hook failed
    await env.settle()
    assert seen == [True, False, False, False]
