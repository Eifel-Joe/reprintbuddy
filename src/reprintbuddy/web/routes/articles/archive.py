"""Archive, restore, restore with another file, pause of the automation and the print file (Spec 5.1, 5.5)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.domain import assembled, catalog, pauses
from reprintbuddy.domain.errors import ConflictError, DomainError, ValidationError
from reprintbuddy.models import Article
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.archiving import add_archive_routes
from reprintbuddy.web.deps import (
    get_or_404,
    current_language,
    get_bambuddy,
    get_db,
    local_next,
    release_connection,
)
from reprintbuddy.web.forms import run_form_action
from reprintbuddy.web.flash import flash
from reprintbuddy.web.filament_view import BAMBUDDY_ERRORS
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.routes.articles.form import LATER, NotSliced, _problem, _read_plate
from reprintbuddy.web.routes.articles.create import _browse, _plates_page
from reprintbuddy.web.routes.articles.detail import _detail_page, _relink_article

router = APIRouter()


# ---- archive, restore, restore with another file (Spec 5.5) ------------------------------------------


async def _after_archive(request: Request, article_id: int) -> str:
    """From the list (its forms send `next`) the person lands on the list again, where the row left or
    came back; from the detail page (no `next`) on the detail page."""
    return local_next(await request.form()) or f"/articles/{article_id}"


def _archive_refused(request: Request, db: Session, article, text: str, status: int, exc: Exception):
    # a taken plate offers the way out: restore with another file
    offer = getattr(exc, "key", None) == "articles.restore_plate_taken"
    return _detail_page(request, db, article, error=text, status=status, relink_offer=offer)


add_archive_routes(
    router, base="/articles", param="article_id", load=lambda db, ident: get_or_404(db, Article, ident),
    archive=catalog.archive_article, restore=catalog.restore_article,
    flash_archived="articles.flash_archived", flash_restored="articles.flash_restored",
    success_url=_after_archive, on_error=_archive_refused,
)


# Choosing the file again: an archived article is restored with it (Spec 5.5), an active one
# keeps its numbers and only changes file and plate (Spec 5.1, plan B4 decision 10).


@router.get("/articles/{article_id:int}/relink")
async def relink_browse(request: Request, article_id: int, folder: str | None = None, db: Session = Depends(get_db)):
    _relink_article(db, article_id)
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers
    base = f"/articles/{article_id}/relink"
    return await _browse(request, folder, base, f"{base}/file", relink=True, article_id=article_id)


@router.get("/articles/{article_id:int}/relink/file/{file_id:int}")
async def relink_plates(request: Request, article_id: int, file_id: int, db: Session = Depends(get_db)):
    archived = _relink_article(db, article_id).archived_at is not None
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers
    base = f"/articles/{article_id}/relink"
    return await _plates_page(
        request, db, file_id, file_base=f"{base}/file", back_base=base, relink_id=article_id,
        relink_archived=archived,
    )


@router.post(
    "/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}",
    dependencies=[Depends(verify_csrf)],
)
async def relink_restore(request: Request, article_id: int, file_id: int, index: int, db: Session = Depends(get_db)):
    article = _relink_article(db, article_id)
    archived, name, made_of_parts = article.archived_at is not None, article.name, article.kind == "assembled"
    base = f"/articles/{article_id}/relink"
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers
    client = get_bambuddy(request)
    if client is None:
        return _problem(request, "articles.bambuddy_missing", relink=True)
    try:  # read fresh from Bambuddy, outside of the write transaction
        read = await _read_plate(client, file_id, index)
    except NotSliced:
        return _problem(request, "articles.not_sliced", 409, relink=True)
    except BAMBUDDY_ERRORS:
        return _problem(request, "articles.bambuddy_error", relink=True)
    if read is None:
        return _problem(request, "articles.file_missing", relink=True)
    file, plate, requirements, thumbnail = read

    again: list[int] = []  # slots the person has to tick again after the change (Spec 5.1)
    changed = False

    def work(tx):
        nonlocal changed
        again.extend(catalog.slots_losing_group(tx, article_id, requirements))  # before the change
        if made_of_parts:  # back to a print article (Spec 5.6 "Wechsel"); archived ones are restored first
            if archived:
                raise ConflictError("articles.edit_archived")
            assembled.convert_to_print(
                tx, article_id, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail,
                actor="user",
            )
        elif archived:
            catalog.restore_article_with_plate(
                tx, article_id, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail
            )
        else:
            catalog.relink_article(
                tx, article_id, file=file, plate=plate, requirements=requirements, thumbnail=thumbnail,
                actor="user",
            )
        changed = True
        return {"name": name}

    response = await run_form_action(
        request,
        work=work,
        # p153: slots to tick again (the note below says which) are ticked on the filament page; a repeat
        # of the form did no work (`changed` stays False) and goes to the article as always
        success_url=lambda: f"/articles/{article_id}/filaments{LATER}" if again and changed else f"/articles/{article_id}",
        flash_key="articles.flash_restored" if archived else "articles.relinked",
        on_error=lambda text, status, exc: _plates_page(
            request, db, file_id, file_base=f"{base}/file", back_base=base, relink_id=article_id,
            relink_archived=archived, error=text, status=status,
        ),
    )
    if again and changed:
        # Only on the real change (a repeated form is answered without work, a refusal shows the page).
        flash(request, "articles.relinked_regroup", warning=True, slots=", ".join(map(str, again)))
    return response


# ---- pause of the automation and the print file (Spec 5.1, plan B4 decision 8) ----------------


def _detail_refused(request: Request, db: Session, article_id: int, text: str, status: int = 200):
    return _detail_page(request, db, get_or_404(db, Article, article_id), error=text, status=status)


@router.post("/articles/{article_id:int}/resume", dependencies=[Depends(verify_csrf)])
async def article_resume(request: Request, article_id: int, db: Session = Depends(get_db)):
    """Lift the pause. A file pause or a missing print target refuses (`pauses.lift_pause`).

    Back to the page the button was on (`next`, a local path; the notice of a pause on the
    overview and the queue has one), else to the article. A refusal shows on the article page."""
    name = get_or_404(db, Article, article_id).name
    form = await request.form()
    # The overview is "/", which `local_next` counts as no `next`: here it is a page like any.
    back = "/" if str(form.get("next", "")) == "/" else local_next(form)
    after = back or f"/articles/{article_id}"

    def work(tx):
        pauses.lift_pause(tx, article_id, "user")
        return {"name": name}

    return await run_form_action(
        request, work=work, success_url=after, flash_key="articles.resumed",
        on_error=lambda text, status, exc: _detail_refused(request, db, article_id, text, status),
    )


@router.post("/articles/{article_id:int}/auto", dependencies=[Depends(verify_csrf)])
async def article_auto(request: Request, article_id: int, db: Session = Depends(get_db)):
    """The automation switch of the list (p44): `enabled=1|0` is the wanted state, never a toggle.

    Back to the list the switch was on (`next`, with its search and filter); a refusal is a
    warning there, not a page of its own."""
    name = get_or_404(db, Article, article_id).name
    form = await request.form()
    after = local_next(form) or "/articles"
    wanted = str(form.get("enabled", "")).strip()

    def work(tx):
        if wanted not in ("0", "1"):
            raise ValidationError("articles.auto_bad_value")
        catalog.set_auto_enabled(tx, article_id, wanted == "1")
        return {"name": name}

    def refused(text: str, status: int, exc: Exception):
        if isinstance(exc, DomainError):
            flash(request, exc.key, warning=True, **{k: v for k, v in exc.params.items() if isinstance(v, (str, int))})
        else:  # an expired or forged form token
            flash(request, "form.expired", warning=True)
        return RedirectResponse(after, status_code=303)

    return await run_form_action(
        request, work=work, success_url=after,
        flash_key="articles.auto_on" if wanted == "1" else "articles.auto_off", on_error=refused,
    )


@router.post("/articles/{article_id:int}/confirm-file", dependencies=[Depends(verify_csrf)])
async def article_confirm_file(request: Request, article_id: int, db: Session = Depends(get_db)):
    """Accept the changed print file as it is now (Spec 5.1): file, filament need and preview
    are read from Bambuddy first, outside of the write transaction."""
    article = get_or_404(db, Article, article_id)
    name, file_id, plate_index = article.name, article.library_file_id, article.plate_id
    release_connection(db)  # nothing is pending; no connection is held while Bambuddy answers

    def refused(key: str):
        return _detail_refused(request, db, article_id, translate(current_language(request), key))

    client = get_bambuddy(request)
    if client is None:
        return refused("articles.bambuddy_missing")
    try:
        read = await _read_plate(client, file_id, plate_index)
    except BAMBUDDY_ERRORS:
        return refused("articles.bambuddy_error")
    if read is None:  # the file or its plate is gone: only choosing again helps
        return refused("articles.file_missing")
    file, _plate, requirements, thumbnail = read

    # Confirming is a way of choosing the file again (Spec 5.1): a slot that is another material or
    # colour in the changed file loses its group, and the person ticks it again on the filament page
    # (p153; the same rule and the same way as `relink_restore`).
    again: list[int] = []
    changed = False

    def work(tx):
        nonlocal changed
        again.extend(catalog.slots_losing_group(tx, article_id, requirements))  # before the change
        catalog.confirm_article_file(
            tx, article_id, file=file, requirements=requirements, thumbnail=thumbnail, actor="user"
        )
        changed = True
        return {"name": name}

    response = await run_form_action(
        request, work=work,
        success_url=lambda: f"/articles/{article_id}/filaments{LATER}" if again and changed else f"/articles/{article_id}",
        flash_key="articles.file_confirmed",
        on_error=lambda text, status, exc: _detail_refused(request, db, article_id, text, status),
    )
    if again and changed:
        flash(request, "articles.relinked_regroup", warning=True, slots=", ".join(map(str, again)))
    return response
