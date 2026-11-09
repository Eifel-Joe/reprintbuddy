"""Bambuddy, messages, uploads and labels in the activity log (Spec 10.7, groups 5–7)."""

from __future__ import annotations

import importlib.util
import pathlib

import httpx
from sqlalchemy import select

from conftest import csrf_of, sm_of
from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy.models import ActivityEntry

_spec = importlib.util.spec_from_file_location(
    "_activity_notify_helpers", pathlib.Path(__file__).resolve().parents[1] / "notify" / "test_notify_service.py"
)
notify_tests = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify_tests)
env = notify_tests.env  # the fixture: Home Assistant switched on, a notifier with a fixed clock


def lines(maker, area: str) -> list[tuple[str, str, dict | None]]:
    with maker() as s:
        rows = s.scalars(select(ActivityEntry).where(ActivityEntry.area == area).order_by(ActivityEntry.id))
        return [(row.kind, row.actor, row.details) for row in rows]


# ---- Bambuddy ---------------------------------------------------------------------------------


def tick(client, poll: bool = True) -> None:
    client.app.state.ticker._tell_after_tick(poll)
    client.app.state.activity.drain()


def test_the_connection_is_logged_as_changes_after_regular_polls(ready_client):
    status = ready_client.app.state.ticker.status
    status.configured, status.reachable = True, True
    tick(ready_client)  # unknown -> ok: nothing
    status.reachable = False
    tick(ready_client, poll=False)  # not a regular poll: nothing
    tick(ready_client)
    tick(ready_client)  # still unreachable: nothing new
    status.reachable, status.poll_error = True, "forbidden"
    tick(ready_client)
    status.poll_error = None
    tick(ready_client)
    found = [(kind, actor, details["state"]) for kind, actor, details in lines(sm_of(ready_client), "bambuddy")]
    assert found == [
        ("bambuddy_connection", "bambuddy", "unreachable"),
        ("bambuddy_connection", "bambuddy", "refused"),
        ("bambuddy_connection", "bambuddy", "ok"),
    ]


def test_missing_rights_and_back(ready_client):
    status = ready_client.app.state.ticker.status
    status.configured, status.reachable = True, True
    rights = ready_client.app.state.rights
    rights.record(ConnectionCheck(ok=False, reason="missing_permissions", missing=("queue:create",)))
    tick(ready_client)
    rights.record(ConnectionCheck(ok=True, reason="ok"))
    tick(ready_client)
    found = [details for kind, _actor, details in lines(sm_of(ready_client), "bambuddy") if kind == "bambuddy_rights"]
    assert found == [{"state": "missing", "missing": ["queue:create"]}, {"state": "ok"}]


# ---- messages ---------------------------------------------------------------------------------


async def test_accepted_messages_each_and_of_failures_only_the_first(env, respx_mock):
    route = respx_mock.post(notify_tests.CALL)
    route.side_effect = [httpx.Response(503), httpx.Response(503), httpx.Response(200, json=[])]
    env.pause()
    for _ in range(3):
        env.notifier.after_tick(False)
        await env.settle()
    found = lines(env.db, "messages")
    assert found == [
        ("message_sent", "system", {"event": "paused", "count": 1, "outcome": "server"}),
        ("message_sent", "system", {"event": "paused", "count": 1, "outcome": "accepted"}),
    ]
    assert "Article paused" not in repr(found) and notify_tests.TOKEN not in repr(found)


async def test_every_accepted_message_is_logged(env, respx_mock):
    respx_mock.post(notify_tests.CALL).respond(200, json=[])
    env.pause("A")
    env.notifier.after_tick(False)
    await env.settle()
    env.pause("B")
    env.notifier.after_tick(False)
    await env.settle()
    assert [details["outcome"] for _k, _a, details in lines(env.db, "messages")] == ["accepted", "accepted"]


def test_the_test_message_is_the_user(ready_client, respx_mock):
    respx_mock.post(notify_tests.CALL).respond(401)
    token = csrf_of(ready_client, "/settings?tab=notify")
    page = ready_client.get("/settings?tab=notify").text
    import re

    form_token = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
    response = ready_client.post(
        "/settings/notify/test",
        data={
            "csrf_token": token, "form_token": form_token, "ha_url": notify_tests.URL,
            "ha_token": notify_tests.TOKEN, "ha_service": "notify.mobile_app_phone",
        },
    )
    assert response.status_code == 200
    ready_client.app.state.activity.drain()
    found = lines(sm_of(ready_client), "messages")
    assert found == [("message_test", "user", {"outcome": "token"})]
    assert notify_tests.TOKEN not in repr(found)


# ---- uploads and labels -----------------------------------------------------------------------


def _helpers(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, pathlib.Path(__file__).resolve().parents[1] / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upload_tests = _helpers("_activity_upload_helpers", "web/test_article_upload.py")
label_tests = _helpers("_activity_label_helpers", "web/test_labels_pages.py")
up = upload_tests.up
net = label_tests.net
configured = label_tests.configured
article = label_tests.article


def test_an_upload_names_file_and_folder(up):
    response = upload_tests.post(up)
    assert response.status_code == 303
    up.app.state.activity.drain()
    assert lines(sm_of(up), "bambuddy") == [
        ("upload", "user", {"name": "New part.gcode.3mf", "folder": "Templates", "result": "uploaded"})
    ]


def test_an_upload_refused_by_bambuddy_without_its_text(up, fake_bambuddy):
    from reprintbuddy.bambuddy.client import BambuddyRejected

    fake_bambuddy.fail_next("upload_file", BambuddyRejected("SECRET-DETAIL", status=403))
    upload_tests.post(up)
    up.app.state.activity.drain()
    found = lines(sm_of(up), "bambuddy")
    assert [details["result"] for _k, _a, details in found] == ["rejected"]
    assert "SECRET-DETAIL" not in repr(found)


def test_a_file_refused_before_sending_is_not_logged(up):
    upload_tests.post(up, name="notes.txt")
    up.app.state.activity.drain()
    assert lines(sm_of(up), "bambuddy") == []


def test_a_test_label_and_an_article_label(configured, net, article):
    done, _ = label_tests.trial(configured)
    assert done.status_code == 200
    path = f"/articles/{article}"
    data = {
        "csrf_token": csrf_of(configured, path),
        "form_token": label_tests.form_token_of(configured, path, f"/articles/{article}/label"),
        "copies": "2",
    }
    configured.post(f"/articles/{article}/label", data=data)
    configured.app.state.activity.drain()
    assert lines(sm_of(configured), "labels") == [
        ("label_test", "user", {"printed": 1, "requested": 1, "result": "printed"}),
        ("label_printed", "user", {"id": article, "name": "Template A", "printed": 2, "requested": 2, "result": "printed"}),
    ]
