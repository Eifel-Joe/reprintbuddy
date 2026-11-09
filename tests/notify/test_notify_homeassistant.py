"""The call to Home Assistant (Spec 10.6 "Weg", "Senden und Fehler", "Testnachricht", "Sicherheit").

Home Assistant is never contacted: every address ends in `.test` and respx answers.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from reprintbuddy.notify import homeassistant as ha

URL = "http://ha.test:8123"
TOKEN = "eyJ0eXAiOiJKV1QifQ.TOKENCHECK.sig"
SERVICE = "notify.mobile_app_phone"
CALL = f"{URL}/api/services/notify/mobile_app_phone"
MESSAGE = ha.Message("Ready for inspection", "1 print waits for inspection", "http://192.0.2.7:8080/inspection")


_TLS = httpx.create_ssl_context()  # built once, as in the app (`shared_tls_client_factory`)


def factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=_TLS)


async def send(message: ha.Message = MESSAGE, **overrides) -> ha.Outcome:
    args = {"url": URL, "token": TOKEN, "service": SERVICE, **overrides}
    return await ha.send(factory, args["url"], args["token"], args["service"], message)


async def test_the_call_is_a_post_to_the_notify_service_with_the_token(respx_mock):
    route = respx_mock.post(CALL).respond(200, json=[])
    assert await send() == "accepted"
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {
        "title": "Ready for inspection",
        "message": "1 print waits for inspection",
        "data": {"url": MESSAGE.link, "clickAction": MESSAGE.link},
    }


async def test_without_a_link_there_is_no_data(respx_mock):
    route = respx_mock.post(CALL).respond(200, json=[])
    assert await send(ha.Message("Title", "Text")) == "accepted"
    assert json.loads(route.calls.last.request.content) == {"title": "Title", "message": "Text"}


async def test_an_address_with_a_path_in_front_keeps_it(respx_mock):
    route = respx_mock.post("http://proxy.test/ha/api/services/notify/mobile_app_phone").respond(200, json=[])
    assert await send(url="http://proxy.test/ha") == "accepted"
    assert route.called


async def test_timeouts_are_five_seconds_to_connect_and_thirty_in_all(respx_mock):
    route = respx_mock.post(CALL).respond(200, json=[])
    await send()
    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["connect"] == ha.CONNECT_TIMEOUT == 5.0
    assert timeout["read"] == ha.TOTAL_TIMEOUT == 30.0


@pytest.mark.parametrize(
    "status,outcome",
    [
        (200, "accepted"),
        (201, "refused"),  # Spec 10.6: "angenommen" when Home Assistant answers 200
        (204, "refused"),
        (401, "token"),
        (400, "request"),
        (403, "blocked"),
        (404, "refused"),
        (405, "refused"),
        (500, "server"),
        (503, "server"),
        (301, "redirect"),
        (302, "redirect"),
        (307, "redirect"),
    ],
)
async def test_each_answer_has_its_fixed_outcome(respx_mock, status, outcome):
    headers = {"Location": "http://elsewhere.test/"} if 300 <= status < 400 else {}
    respx_mock.post(CALL).respond(status, headers=headers, text="whatever Home Assistant says")
    assert await send() == outcome


async def test_a_redirect_is_not_followed(respx_mock):
    respx_mock.post(CALL).respond(302, headers={"Location": "http://elsewhere.test/steal"})
    elsewhere = respx_mock.route(host="elsewhere.test").respond(200)
    assert await send() == "redirect"
    assert not elsewhere.called  # the token never travels to another address


@pytest.mark.parametrize(
    "error,outcome",
    [
        (httpx.ConnectError("refused"), "unreachable"),
        (httpx.ConnectTimeout("slow"), "unreachable"),
        (httpx.PoolTimeout("busy"), "unreachable"),
        (httpx.ReadTimeout("slow"), "unclear"),  # Home Assistant may still send it
        (httpx.WriteTimeout("slow"), "unclear"),
        (httpx.RemoteProtocolError("cut"), "unclear"),
        (httpx.ReadError("cut"), "unclear"),
        # The request never went out: not reachable rather than unclear (final review 7).
        (httpx.UnsupportedProtocol("no"), "unreachable"),
        (httpx.ProxyError("no"), "unreachable"),
        (httpx.LocalProtocolError("no"), "unreachable"),
    ],
)
async def test_network_errors(respx_mock, error, outcome):
    respx_mock.post(CALL).mock(side_effect=error)
    assert await send() == outcome


async def test_the_whole_call_has_a_time_limit(respx_mock, monkeypatch):
    monkeypatch.setattr(ha, "TOTAL_TIMEOUT", 0.05)

    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    respx_mock.post(CALL).mock(side_effect=hang)
    assert await send() == "unclear"


@pytest.mark.parametrize("bad", ["light.turn_on", "notify.send_message", "notify/../x", ""])
async def test_a_service_outside_the_notify_domain_is_never_called(respx_mock, bad):
    with pytest.raises(ValueError):
        await send(service=bad)
    assert respx_mock.calls.call_count == 0


async def test_neither_token_nor_answer_text_appear_in_the_log(respx_mock, caplog):
    caplog.set_level(logging.DEBUG)
    respx_mock.post(CALL).respond(400, text="ANSWERCHECK " + "x" * 500)
    assert await send() == "request"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "TOKENCHECK" not in logged
    # The answer goes to the debug log only, cut short.
    answer_lines = [r for r in caplog.records if "ANSWERCHECK" in r.getMessage()]
    assert answer_lines and all(r.levelno == logging.DEBUG for r in answer_lines)
    assert all(len(r.getMessage()) < 400 for r in answer_lines)


async def test_ping_asks_the_api_root(respx_mock):
    route = respx_mock.get(f"{URL}/api/").respond(200, json={"message": "API running."})
    assert await ha.ping(factory, URL, TOKEN) == "accepted"
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_ping_with_a_wrong_token(respx_mock):
    respx_mock.get(f"{URL}/api/").respond(401)
    assert await ha.ping(factory, URL, TOKEN) == "token"


def test_outcomes_are_a_closed_list():
    assert ha.OUTCOMES == (
        "accepted", "token", "request", "blocked", "server", "redirect", "unreachable", "unclear", "refused",
    )


@pytest.mark.parametrize("url", ["http://xn--a:8123", "http://[::1:8123"])
async def test_an_address_httpx_cannot_use_is_not_reachable_and_raises_nothing(respx_mock, url):
    """Final review 1: the settings accept it, httpx refuses it (`InvalidURL`, an IDNA error). The
    caller must get an outcome, never an exception: a test button answers with a page, the sending
    task records the failure and shows the warning."""
    assert await send(url=url) == "unreachable"
    assert respx_mock.calls.call_count == 0
