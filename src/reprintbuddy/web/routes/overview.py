"""Start page. B7 builds the real overview; until then it shows the layout and the warnings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from reprintbuddy.web.deps import get_db, render
from reprintbuddy.web.engine_view import warnings_context

router = APIRouter()


@router.get("/")
async def overview(request: Request, db: Session = Depends(get_db)):
    return render(request, "overview.html", active_nav="overview", **warnings_context(request, db))
