"""Start page. Placeholder until Task 7 adds the layout; B7 builds the real overview."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/")
async def overview() -> HTMLResponse:
    return HTMLResponse("<!doctype html><title>Lagerbuddy</title><p>Lagerbuddy</p>")
