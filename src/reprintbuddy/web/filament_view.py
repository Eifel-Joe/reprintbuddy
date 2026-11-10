"""Shared by the pages that show filament: the spools now, and the colour swatch rule."""

from __future__ import annotations

import re

from fastapi import Request

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.web.deps import get_bambuddy

# Bambuddy cannot be reached, or refuses (4xx: bad key, missing right): pages go on without it.
BAMBUDDY_ERRORS = (BambuddyUnavailable, BambuddyRejected)

COLOR = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")


# p230/p231 (v0.3.15). Root: the reorder page, the filament page and the filament mapping of an article
# entered new spool sorts and closed arrived "ordered" marks whenever they were opened, also while writes are
# paused for a restore or an update (only the forms are refused then, by `update_guard`; a GET is no form).
# After a restore the old process could so write into the restored database before its restart. And they
# asked Bambuddy for the spools even when the last query found it unreachable, waiting for the timeout before
# showing the record. The overview has done both right since v0.3.14; these pages now follow the same rule:
# `may_write` is the rule of the activity log, and while it says no the page reads the record only.
# Rejected: refusing the pages while writes are paused - they are read-only views the user may still want.
# Tests: tests/web/test_filament_pages_guarded.py.


def may_write(request: Request) -> bool:
    """May a page write now? No while a restore or an update runs (the rule of the activity log)."""
    return bool(request.app.state.activity.may_write())


async def page_spools(request: Request):
    """The spools for a page that shows the stock: None (the page shows the record) while writes are paused,
    since live spools enter new sorts, and when the last query found Bambuddy unreachable, so the page does
    not wait for its timeout to show the record anyway."""
    if not may_write(request):
        return None
    return await spools_or_none(request)


async def spools_or_none(request: Request):
    """The spools now; None when Bambuddy is not configured or cannot be reached or refuses (never a 500)."""
    client = get_bambuddy(request)
    if client is None:
        return None
    try:
        return await client.list_spools()
    except BAMBUDDY_ERRORS:
        return None
