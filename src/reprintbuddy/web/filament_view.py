"""Shared by the pages that show filament: the spools now, and the colour swatch rule."""

from __future__ import annotations

import re

from fastapi import Request

from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.web.deps import get_bambuddy

COLOR = re.compile(r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?")


async def spools_or_none(request: Request):
    """The spools now; None when Bambuddy is not configured or cannot be reached (never a 500)."""
    client = get_bambuddy(request)
    if client is None:
        return None
    try:
        return await client.list_spools()
    except BambuddyUnavailable:
        return None
