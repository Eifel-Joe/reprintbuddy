"""The buttons of the missing-rights notice: check again, later, and continue once every
right is there (Spec 11.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.web.bambuddy_check import run_check
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import (
    RIGHTS_LATER_KEY,
    RIGHTS_SUCCESS_KEY,
    get_db,
    safe_next,
    stored_credentials,
    take_verdict,
)
from reprintbuddy.web.flash import flash

router = APIRouter()


@router.post("/bambuddy/rights/recheck", dependencies=[Depends(verify_csrf)])
async def recheck(request: Request, db: Session = Depends(get_db)):
    """Ask Bambuddy again with the stored address and key, and go back to the page.

    From the dialog (`from=dialog`) a success keeps the dialog open, now with every right
    ticked, until "Continue" (p31). From the warning line after "Later" it is a message."""
    form = await request.form()
    target = safe_next(str(form.get("next") or ""))
    credentials = stored_credentials(request.app)
    if credentials is None:
        return RedirectResponse(target, status_code=303)
    check = await run_check(request, db, *credentials)
    take_verdict(request, check)
    if check.ok and form.get("from") == "dialog":
        request.session[RIGHTS_SUCCESS_KEY] = True  # the green dialog says it: no message as well
    elif check.ok:
        flash(request, "rights.ok")
    elif check.reason == "missing_permissions":
        flash(request, "rights.still_missing", warning=True)  # so the person sees that Bambuddy was asked
    else:
        flash(request, f"bambuddy.reason.{check.reason}", warning=True)  # nothing could be checked: say why
    return RedirectResponse(target, status_code=303)


@router.post("/bambuddy/rights/later", dependencies=[Depends(verify_csrf)])
async def later(request: Request):
    """Replace the dialog with the warning line until the person signs out, for these rights."""
    form = await request.form()
    notice = request.app.state.rights.notice
    if notice is not None:
        request.session[RIGHTS_LATER_KEY] = ",".join(notice.missing)
    return RedirectResponse(safe_next(str(form.get("next") or "")), status_code=303)


@router.post("/bambuddy/rights/done", dependencies=[Depends(verify_csrf)])
async def done(request: Request):
    """"Continue" in the green dialog: close it and stay on the page."""
    form = await request.form()
    request.session.pop(RIGHTS_SUCCESS_KEY, None)
    return RedirectResponse(safe_next(str(form.get("next") or "")), status_code=303)
