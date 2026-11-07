"""Small helpers of the interface itself: folding the menu without JavaScript."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from reprintbuddy.web.deps import safe_next

router = APIRouter()

COOKIE = "rb_sidebar"
_COOKIE_AGE = 365 * 24 * 3600


@router.get("/ui/sidebar")
async def sidebar_toggle(request: Request, collapsed: str = "0", next: str = "/") -> RedirectResponse:
    """Remember whether the menu is folded (cookie) and go back to the page. The gate has already
    sent a person who is not signed in to the sign-in page."""
    response = RedirectResponse(safe_next(next), status_code=303)
    if collapsed == "1":
        response.set_cookie(
            COOKIE,
            "collapsed",
            max_age=_COOKIE_AGE,
            path="/",
            samesite="lax",
            httponly=False,  # sidebar.js sets it too
            secure=request.url.scheme == "https",
        )
    else:
        response.delete_cookie(COOKIE, path="/")
    return response
