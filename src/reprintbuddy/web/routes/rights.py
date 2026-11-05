"""The two buttons of the missing-rights notice: check again, and later (Spec 11.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.web.bambuddy_check import run_check
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db, safe_next, stored_credentials
from reprintbuddy.web.flash import flash

router = APIRouter()

LATER_KEY = "rights_later"  # in the session: gone with the sign-out, and with the next sign-in


@router.post("/bambuddy/rights/recheck", dependencies=[Depends(verify_csrf)])
async def recheck(request: Request, db: Session = Depends(get_db)):
    """Ask Bambuddy again with the stored address and key, and go back to the page."""
    form = await request.form()
    target = safe_next(str(form.get("next") or ""))
    credentials = stored_credentials(request.app)
    if credentials is None:
        return RedirectResponse(target, status_code=303)
    check = await run_check(request, db, *credentials)
    request.app.state.rights.record(check)
    if check.ok:
        request.session.pop(LATER_KEY, None)
        flash(request, "rights.ok")
    elif check.reason != "missing_permissions":
        flash(request, f"bambuddy.reason.{check.reason}")  # nothing could be checked: say why
    return RedirectResponse(target, status_code=303)


@router.post("/bambuddy/rights/later", dependencies=[Depends(verify_csrf)])
async def later(request: Request):
    """Replace the dialog with the warning line until the person signs out."""
    form = await request.form()
    request.session[LATER_KEY] = True
    return RedirectResponse(safe_next(str(form.get("next") or "")), status_code=303)
