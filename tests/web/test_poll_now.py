"""The button "Query Bambuddy now" (v0.3.6 p87): queue page and overview.

A POST with CSRF token and one-time token asks the tick engine for a regular poll
(`Engine.request_poll`); the answer comes live through the regions that exist already. Without a
set-up Bambuddy there is no button, and a POST does nothing. While an update runs it is paused
like every other write.
"""

import re

import pytest

from conftest import csrf_of, form_token_of
from reprintbuddy.web.i18n import translate

ACTION = "/queue/poll"


@pytest.fixture
def polls(bb_client):
    calls: list[str] = []
    bb_client.app.state.ticker.request_poll = lambda: calls.append("poll")
    return calls


def poll_form(page: str) -> str:
    forms = re.findall(rf'<form[^>]*action="{ACTION}"[^>]*>(.*?)</form>', page, re.S)
    assert forms, "no poll form"
    return forms[0]


def post_poll(client, page="/queue", **extra):
    """Submit the form of `page` as the browser would, with its own hidden `next`."""
    form = poll_form(client.get(page).text)
    data = {"csrf_token": csrf_of(client, page), "form_token": form_token_of(client, page, ACTION)}
    data["next"] = re.search(r'name="next" value="([^"]*)"', form).group(1)
    data.update(extra)
    return client.post(ACTION, data=data)


@pytest.mark.parametrize("page", ["/queue", "/"])
def test_button_on_both_pages_with_bambuddy(bb_client, page):
    html = poll_form(bb_client.get(page).text)
    assert translate("en", "queue.poll_now") in html
    assert 'name="csrf_token"' in html and 'name="form_token"' in html


@pytest.mark.parametrize("page", ["/queue", "/"])
def test_no_button_without_bambuddy(ready_client, page):
    assert ACTION not in ready_client.get(page).text


def test_post_asks_for_one_poll_and_says_so(bb_client, polls):
    response = post_poll(bb_client)
    assert response.status_code == 303
    assert response.headers["location"] == "/queue"
    assert polls == ["poll"]
    assert translate("en", "queue.poll_requested") in bb_client.get("/queue").text


def test_overview_form_returns_to_the_overview(bb_client, polls):
    response = post_poll(bb_client, "/")
    assert response.headers["location"] == "/"
    assert polls == ["poll"]


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example", "/settings", "javascript:1", ""])
def test_return_address_is_a_whitelist(bb_client, polls, target):
    response = post_poll(bb_client, next=target)
    assert response.headers["location"] == "/queue"


def test_double_submit_of_the_same_form_asks_once(bb_client, polls):
    data = {"csrf_token": csrf_of(bb_client, "/queue"), "form_token": form_token_of(bb_client, "/queue", ACTION)}
    first = bb_client.post(ACTION, data=data)
    second = bb_client.post(ACTION, data=data)
    assert first.status_code == second.status_code == 303
    assert polls == ["poll"]
    assert translate("en", "form.duplicate") in bb_client.get("/queue").text


def test_without_csrf_token_nothing_happens(bb_client, polls):
    response = bb_client.post(ACTION, data={"form_token": form_token_of(bb_client, "/queue", ACTION)})
    assert response.status_code == 403
    assert polls == []


def test_without_form_token_nothing_happens(bb_client, polls):
    response = bb_client.post(ACTION, data={"csrf_token": csrf_of(bb_client, "/queue")})
    assert polls == []
    assert response.status_code == 303
    assert translate("en", "form.expired") in bb_client.get("/queue").text


@pytest.mark.parametrize("page", ["/queue", "/"])
def test_a_forged_form_token_goes_back_to_the_page_it_came_from(bb_client, polls, page):
    """Review of v0.3.6: the notice about an expired form belongs on the page of the button."""
    form = poll_form(bb_client.get(page).text)
    back = re.search(r'name="next" value="([^"]*)"', form).group(1)
    data = {"csrf_token": csrf_of(bb_client, page), "form_token": "forged", "next": back}
    response = bb_client.post(ACTION, data=data)
    assert response.status_code == 303
    assert response.headers["location"] == page
    assert polls == []
    assert translate("en", "form.expired") in bb_client.get(page).text


def test_post_without_bambuddy_asks_for_nothing(ready_client):
    calls: list[str] = []
    ready_client.app.state.ticker.request_poll = lambda: calls.append("poll")
    token = csrf_of(ready_client, "/queue")
    response = ready_client.post(ACTION, data={"csrf_token": token, "form_token": "x"})
    assert response.status_code == 303
    assert calls == []
    assert translate("en", "queue.poll_unavailable") in ready_client.get("/queue").text


def test_paused_while_an_update_runs(bb_client, polls, monkeypatch):
    from reprintbuddy.web.routes import ops

    data = {"csrf_token": csrf_of(bb_client, "/queue"), "form_token": form_token_of(bb_client, "/queue", ACTION)}
    monkeypatch.setattr(ops, "update_running", lambda *a, **k: True)
    response = bb_client.post(ACTION, data=data)
    assert response.status_code == 503
    assert polls == []
    assert ACTION not in ops.UPDATE_EXEMPT_PATHS


def test_the_poll_goes_through_the_engine_flag(bb_client):
    """Not replaced: the real `request_poll` sets the flag that `run` reads."""
    ticker = bb_client.app.state.ticker
    assert ticker._poll_requested is False
    post_poll(bb_client)
    assert ticker._poll_requested is True
