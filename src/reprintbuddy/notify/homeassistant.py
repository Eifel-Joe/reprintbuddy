"""The one call to Home Assistant (Spec 10.6 "Weg", "Senden und Fehler", "Sicherheit").

`POST <address>/api/services/notify/<service>` with the long-lived access token, body `title`,
`message` and, when there is one, the link for a tap (`data.url` for iOS, `data.clickAction` for
Android). The answer becomes one of the fixed `OUTCOMES`; Home Assistant's own text never leaves
this module except to the debug log, cut short, so neither the test button nor a page can be used
to read what some address answers (the same reason as in Bambuddy, `notification_service.py:147`).

- "accepted" only means Home Assistant took the message: whether the phone gets it, only Home
  Assistant knows (its relay errors go to its own log, `mobile_app/notify.py:252-258` @2026.11.0).
- A timeout after the request went out is "unclear": Home Assistant finishes a service call even
  when the caller is gone (`api/__init__.py:446-455`), so the message may still arrive.
- Redirects are never followed: the token goes to the entered address and nowhere else.
- The service must be of the `notify` domain (settings `_ha_service`); it is checked again here, so
  no other service of Home Assistant can ever be called with the token.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import httpx

from reprintbuddy import settings as st

log = logging.getLogger(__name__)

Outcome = Literal[
    "accepted", "token", "request", "blocked", "server", "redirect", "unreachable", "unclear", "refused",
]
OUTCOMES: tuple[str, ...] = (
    "accepted", "token", "request", "blocked", "server", "redirect", "unreachable", "unclear", "refused",
)

CONNECT_TIMEOUT = 5.0  # seconds; as in Bambuddy (#2770)
TOTAL_TIMEOUT = 30.0  # seconds for the whole call (Spec 10.6)
_ANSWER_IN_LOG = 200  # characters of Home Assistant's answer in the debug log

HttpFactory = Callable[[], httpx.AsyncClient]


@dataclass(frozen=True)
class Message:
    title: str
    text: str
    link: str | None = None


def call_path(service: str) -> str:
    """`notify.mobile_app_phone` -> `/api/services/notify/mobile_app_phone`; anything else raises."""
    name = st.validate("ha_service", service)
    if not isinstance(name, str):
        raise ValueError("expected a service of the notify domain")
    return "/api/services/notify/" + name.split(".", 1)[1]


def _outcome(status: int) -> Outcome:
    if 200 <= status < 300:
        return "accepted"
    if 300 <= status < 400:
        return "redirect"
    if status == 401:
        return "token"
    if status == 400:
        return "request"
    if status == 403:
        return "blocked"
    if status >= 500:
        return "server"
    return "refused"


async def _call(
    http_factory: HttpFactory, method: str, url: str, token: str, path: str, body: dict | None
) -> Outcome:
    target = f"{url.rstrip('/')}{path}"
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT):
            async with http_factory() as http:
                response = await http.request(
                    method,
                    target,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=httpx.Timeout(TOTAL_TIMEOUT, connect=CONNECT_TIMEOUT),
                    follow_redirects=False,
                )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
        log.info("Home Assistant %s %s: not reachable (%s)", method, path, type(exc).__name__)
        return "unreachable"
    except (httpx.HTTPError, TimeoutError) as exc:
        # The request may have gone out: Home Assistant can still deliver the message.
        log.info("Home Assistant %s %s: answer unclear (%s)", method, path, type(exc).__name__)
        return "unclear"
    outcome = _outcome(response.status_code)
    log.info("Home Assistant %s %s -> %s (%s)", method, path, response.status_code, outcome)
    if outcome != "accepted":
        log.debug("Home Assistant answered: %s", response.text[:_ANSWER_IN_LOG])
    return outcome


async def send(http_factory: HttpFactory, url: str, token: str, service: str, message: Message) -> Outcome:
    """Send `message` to `service`; raises ValueError for a service outside the notify domain."""
    path = call_path(service)
    body: dict[str, object] = {"title": message.title, "message": message.text}
    if message.link:
        body["data"] = {"url": message.link, "clickAction": message.link}
    return await _call(http_factory, "POST", url, token, path, body)


async def ping(http_factory: HttpFactory, url: str, token: str) -> Outcome:
    """`GET /api/`: answers only with a valid token and needs no admin right (Spec 10.6)."""
    return await _call(http_factory, "GET", url, token, "/api/", None)
