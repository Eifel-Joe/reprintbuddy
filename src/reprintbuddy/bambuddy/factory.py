"""Builds the Bambuddy client from the stored settings (B2 Decision 15).

Every call reads address and key afresh, so a changed setting takes effect at once and
nothing is cached. The read happens in its own short session, which only reads (it never
commits, so it sets nothing off in the change feed) and is closed before the client is
returned.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Callable

import httpx
from sqlalchemy.orm import Session, sessionmaker

from reprintbuddy import settings
from reprintbuddy.bambuddy.client import BambuddyFactory, HttpBambuddyClient


def make_http_factory(
    sessionmaker: sessionmaker[Session],
    http: Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]],
) -> BambuddyFactory:
    def factory() -> HttpBambuddyClient | None:
        with sessionmaker() as session:
            if not settings.bambuddy_configured(session):
                return None
            url = str(settings.get_setting(session, "bambuddy_url"))
            key = str(settings.get_setting(session, "bambuddy_api_key"))
        return HttpBambuddyClient(url, key, http)

    return factory
