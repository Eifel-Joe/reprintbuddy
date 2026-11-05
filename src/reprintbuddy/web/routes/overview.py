"""Start page. B7 builds the real overview; for now it only shows the layout."""

from __future__ import annotations

from fastapi import APIRouter, Request

from reprintbuddy.web.deps import render

router = APIRouter()


@router.get("/")
async def overview(request: Request):
    return render(request, "overview.html", active_nav="overview")
