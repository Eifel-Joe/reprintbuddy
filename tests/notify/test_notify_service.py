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


@pytest.mark.parametrize("outcome_status", [503, 403])  # 403: already banned, an ordinary failure (p152)
async def test_a_failed_state_shows_the_warning_and_is_sent_again(env, respx_mock, outcome_status):
    route = respx_mock.post(CALL)
    route.side_effect = [httpx.Response(outcome_status), httpx.Response(200, json=[])]
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem in ("server", "blocked")
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


# ---- final review 2 and 3: results kept message by message; switched off stays off ----------------


def restarted(env) -> Notifier:
    """A new process on the same database (the status in memory is gone, the marks are not)."""
    return Notifier(
        env.db, http_factory=factory, engine_status=lambda: env.status, rights=lambda: ev.Rights("ok", ()),
        update_started=lambda: False, base=lambda s: None,
    )


async def _settle_of(notifier) -> None:
    for _ in range(200):
        if notifier.task is None or notifier.task.done():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the sending task did not end")


async def test_an_abort_after_one_kind_keeps_what_went_through(env, respx_mock):
    """Final review 2: kind 1 accepted, kind 2 hangs, the process stops: after the restart only kind 2
    goes out (Spec 10.6: a restart sends nothing twice)."""
    messages: list[str] = []
    hang = asyncio.Event()

    async def answer(request):
        body = json.loads(request.content)
        messages.append(body["title"])
        if body["title"] == "Below minimum stock" and not hang.is_set():
            await asyncio.sleep(60)
        return httpx.Response(200, json=[])

    respx_mock.post(CALL).mock(side_effect=answer)
    env.pause("Bracket")
    with env.db.begin() as s:
        make_article(s, name="Cover", min_stock=2)
    env.notifier.after_tick(False)
    for _ in range(100):
        if len(messages) == 2:
            break
        await asyncio.sleep(0.01)
    await env.notifier.stop()
    assert messages == ["Article paused", "Below minimum stock"]
    hang.set()
    again = restarted(env)
    again.after_tick(False)
    await _settle_of(again)
    assert messages == ["Article paused", "Below minimum stock", "Below minimum stock"]


async def test_a_print_is_marked_before_it_is_sent(env, respx_mock):
    """Final review 2: events 1 and 2 are never tried again, so they are marked before the call: an
    abort during the call cannot bring them back after a restart."""
    calls: list[str] = []

    async def hanging(request):
        calls.append(json.loads(request.content)["title"])
        await asyncio.sleep(60)
        return httpx.Response(200, json=[])

    respx_mock.post(CALL).mock(side_effect=hanging)
    with env.db.begin() as s:
        from factories import make_dispatch

        make_dispatch(s, make_article(s).id, status="completed", finished_at=NOW)
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    await env.notifier.stop()
    assert calls == ["Ready for inspection"]
    again = restarted(env)
    again.after_tick(False)
    await _settle_of(again)
    assert calls == ["Ready for inspection"]


async def test_switching_off_during_a_hanging_message_keeps_the_warning_off(env, respx_mock):
    """Final review 3: Spec 10.6, the warning goes when the switch is off - also when a message that
    was on its way fails only afterwards."""
    release = asyncio.Event()

    async def hanging(request):
        await release.wait()
        return httpx.Response(503)

    respx_mock.post(CALL).mock(side_effect=hanging)
    env.pause()
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    with env.db.begin() as s:
        st.set_setting(s, "ha_enabled", False, commit=False)
    env.notifier.after_tick(False)
    release.set()
    await env.settle()
    assert env.notifier.status.problem is None


# ---- p152: a rejected token stops every automatic attempt (Spec 10.6 "Token abgelehnt (401)") -----------


def _rejected(env) -> bool:
    with env.db() as s:
        return st.get_setting(s, "ha_token_rejected") is True


def _saved_again(env, notifier=None) -> None:
    """What saving the card does: the flag goes, the notifier hears of it."""
    with env.db.begin() as s:
        st.set_setting(s, "ha_token_rejected", False, commit=False)
    (notifier or env.notifier).settings_saved()


async def test_a_rejected_token_stops_sending_and_checking(env, respx_mock):
    """Each further try with that token counts in Home Assistant as a failed login (IP ban)."""
    call = respx_mock.post(CALL).respond(401)
    ping = respx_mock.get(PING).respond(401)
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert call.call_count == 1
    assert env.notifier.status.problem == "token"
    assert _rejected(env)
    for poll in (False, True, True):  # short ticks and regular queries alike
        env.notifier.after_tick(poll)
        await env.settle()
    assert call.call_count == 1
    assert ping.call_count == 0
    assert env.marks() == [("paused", "due")]  # the state is still to be reported once allowed again


async def test_a_rejected_token_ends_the_round_at_once(env, respx_mock):
    call = respx_mock.post(CALL).respond(401)
    env.pause("Bracket")
    with env.db.begin() as s:
        make_article(s, name="Cover", min_stock=2)  # a second kind due in the same round
    env.notifier.after_tick(False)
    await env.settle()
    assert call.call_count == 1


async def test_a_rejected_check_stops_too(env, respx_mock):
    respx_mock.post(CALL).respond(503)
    ping = respx_mock.get(PING).respond(401)
    with env.db.begin() as s:
        st.set_setting(s, "ha_events", ["print_done"], commit=False)
        s.add(NotifyMark(kind="print_done", ref="99", state="due", first_seen_at=NOW))
    env.notifier.after_tick(False)
    await env.settle()
    env.notifier.after_tick(True)
    await env.settle()
    assert ping.call_count == 1
    assert _rejected(env)
    assert env.notifier.status.problem == "token"
    env.notifier.after_tick(True)
    await env.settle()
    assert ping.call_count == 1


async def test_the_stop_holds_over_a_restart(env, respx_mock):
    call = respx_mock.post(CALL).respond(401)
    ping = respx_mock.get(PING).respond(401)
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    await env.notifier.stop()
    again = restarted(env)
    again.after_tick(True)
    await _settle_of(again)
    assert call.call_count == 1
    assert ping.call_count == 0
    assert again.status.problem == "token"  # the warning is there again after the restart


async def test_saving_the_card_lifts_the_stop_and_the_warning(env, respx_mock):
    call = respx_mock.post(CALL)
    call.side_effect = [httpx.Response(401), httpx.Response(200, json=[])]
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    assert env.notifier.status.problem == "token"
    _saved_again(env)
    assert env.notifier.status.problem is None
    env.notifier.after_tick(False)
    await env.settle()
    assert call.call_count == 2  # the state that still holds goes out now
    assert env.marks() == [("paused", "sent")]


async def test_prints_finished_during_the_stop_are_not_caught_up(env, respx_mock):
    from factories import make_dispatch

    call = respx_mock.post(CALL)
    call.side_effect = [httpx.Response(401), httpx.Response(200, json=[])]
    env.pause()
    env.notifier.after_tick(False)
    await env.settle()
    with env.db.begin() as s:
        make_dispatch(s, make_article(s, name="Cover").id, status="completed", finished_at=NOW)
    env.notifier.after_tick(False)
    await env.settle()
    assert call.call_count == 1
    _saved_again(env)
    env.notifier.after_tick(False)
    await env.settle()
    titles = [json.loads(c.request.content)["title"] for c in call.calls]
    assert titles == ["Article paused", "Article paused"]  # no "Ready for inspection" afterwards


async def test_a_rejection_of_a_token_replaced_meanwhile_stops_nothing(env, respx_mock):
    """Saved while a message with the old token was on its way: its 401 must not stop the new token."""
    release = asyncio.Event()

    async def hanging(request):
        await release.wait()
        return httpx.Response(401)

    respx_mock.post(CALL).mock(side_effect=hanging)
    env.pause()
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    with env.db.begin() as s:
        st.set_setting(s, "ha_token", "eyJ.replaced.sig", commit=False)
    _saved_again(env)
    release.set()
    await env.settle()
    assert not _rejected(env)
    assert env.notifier.status.problem != "token"


@pytest.mark.parametrize(
    "change", [("ha_service", "notify.other_phone"), ("ha_url", "http://HA.test:8123/")], ids=["service", "spelling"]
)
async def test_a_rejection_of_the_same_token_stops_even_if_other_fields_were_saved(env, respx_mock, change):
    """Review: only the token and the address (as `same_address` sees it) decide whether the refused
    token is still the stored one."""
    release = asyncio.Event()

    async def hanging(request):
        await release.wait()
        return httpx.Response(401)

    respx_mock.post(CALL).mock(side_effect=hanging)
    env.pause()
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    with env.db.begin() as s:
        st.set_setting(s, change[0], change[1], commit=False)
    release.set()
    await env.settle()
    assert _rejected(env)


# ---- p270: a restore waits a little for a message on its way --------------------------------------


async def test_a_restore_waits_for_a_message_on_its_way_and_its_outcome_is_kept(env, respx_mock):
    """Home Assistant accepts while the restore has begun: the mark is written before the copy is made,
    so the restored database (with the marks carried over) does not send it again after the restart."""
    answer = asyncio.Event()

    async def slow(request):
        await answer.wait()
        return httpx.Response(200, json=[])

    route = respx_mock.post(CALL).mock(side_effect=slow)
    env.pause()
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    env.updating = True  # the restore has begun
    settle = asyncio.ensure_future(env.notifier.settle(5.0))
    await asyncio.sleep(0.05)
    assert not settle.done()  # it waits for the answer
    answer.set()
    assert await settle is True
    assert ("paused", "sent") in env.marks()
    await env.settle()
    assert route.call_count == 1


async def test_a_restore_waits_only_so_long_and_a_late_answer_is_not_written(env, respx_mock):
    answer = asyncio.Event()

    async def slow(request):
        await answer.wait()
        return httpx.Response(200, json=[])

    respx_mock.post(CALL).mock(side_effect=slow)
    env.pause()
    env.notifier.after_tick(False)
    await asyncio.sleep(0.05)
    env.updating = True
    assert await env.notifier.settle(0.1) is False
    answer.set()  # the database may be being replaced now: not touched
    await env.settle()
    assert env.marks() == [("paused", "due")]


async def test_a_restore_with_no_message_on_its_way_does_not_wait(env):
    assert await asyncio.wait_for(env.notifier.settle(5.0), 0.5) is True

