"""Connection check for the setup page and the settings page.

B2 moves this function into the Bambuddy client. The endpoint is a read-only
GET; the API key is only ever sent in the header and never logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

Reason = Literal["ok", "unreachable", "unauthorized", "forbidden", "not_bambuddy"]

TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    reason: Reason


async def check_connection(url: str, api_key: str, client: httpx.AsyncClient) -> ConnectionCheck:
    """Can Reprintbuddy reach Bambuddy and read the queue with this key?"""
    try:
        response = await client.get(
            f"{url.rstrip('/')}/api/v1/queue/",
            headers={"X-API-Key": api_key},
            timeout=TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return ConnectionCheck(False, "unreachable")

    status = response.status_code
    if status == 401:
        return ConnectionCheck(False, "unauthorized")
    if status == 403:
        return ConnectionCheck(False, "forbidden")
    if status >= 500:
        return ConnectionCheck(False, "unreachable")
    if status == 200:
        try:
            body = response.json()
        except ValueError:
            return ConnectionCheck(False, "not_bambuddy")
        if isinstance(body, list):
            return ConnectionCheck(True, "ok")
    return ConnectionCheck(False, "not_bambuddy")
