"""Uploading a print file to Bambuddy, then on to the plate and the article (Spec 5.1a).

The only write besides the queue (Spec 3.1). Reprintbuddy cannot delete a file in Bambuddy, so
everything is checked before anything is sent: the length before the body is read, the token,
the right to upload, the folder (it exists and is not external), the name, the content (a zip
with G-code) and that no file of that name is in the folder. One upload at a time
(`upload_lock`): two clicks cannot both pass the name check. An unclear answer is never sent
again; the file is looked for instead.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyClient, BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy.bambuddy.types import LibraryFile, LibraryFolder
from reprintbuddy.domain.dispatch import CLOCK_TOLERANCE
from reprintbuddy.domain.printfile import (
    MAX_UPLOAD_BYTES,
    PrintFileError,
    check_sliced_3mf,
    is_external_folder,
    name_key,
    upload_name,
)
from reprintbuddy.web.bambuddy_check import run_check
from reprintbuddy.web.bodylimit import MULTIPART_OVERHEAD, upload_length_ok
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_bambuddy, get_db, release_connection, render, stored_credentials, take_verdict
from reprintbuddy.web.filament_view import BAMBUDDY_ERRORS
from reprintbuddy.web.flash import flash
from reprintbuddy.web.forms import bounded_int

log = logging.getLogger(__name__)
router = APIRouter()

PATH = "/articles/new/upload"
FIELD = "print_file"
BODY_LIMIT = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD
MAX_DETAIL = 200


def make_lock() -> asyncio.Lock:
    """The app-wide lock around the name check and the send (`app.state.upload_lock`)."""
    return asyncio.Lock()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # Bambuddy's times are naive UTC


def _query(folder_id: int | None) -> str:
    return "" if folder_id is None else f"?folder={folder_id}"


def _crumbs(folders: list[LibraryFolder], folder_id: int | None) -> list[LibraryFolder]:
    by_id = {f.id: f for f in folders}
    crumbs, seen = [], set()
    current = by_id.get(folder_id) if folder_id is not None else None
    while current is not None and current.id not in seen:
        seen.add(current.id)
        crumbs.append(current)
        current = by_id.get(current.parent_id) if current.parent_id is not None else None
    crumbs.reverse()
    return crumbs


def _page(request: Request, *, state: str, status: int = 200, folder_id: int | None = None,
          crumbs: list[LibraryFolder] | None = None, error: str | None = None, **params: object):
    """The upload page. `state`: form, no_right, external or problem; `error` a message key."""
    return render(
        request,
        "article_upload.html",
        status_code=status,
        active_nav="articles",
        state=state,
        error=error,
        error_params=params,
        crumbs=crumbs or [],
        folder_id=folder_id,
        action=PATH + _query(folder_id),
        back=f"/articles/new{_query(folder_id)}",
        max_mb=MAX_UPLOAD_BYTES // (1024 * 1024),
    )


async def _rights(request: Request, db: Session) -> ConnectionCheck | None:
    """Ask Bambuddy which rights the stored key can use; None when nothing could be checked."""
    credentials = stored_credentials(request.app)
    if credentials is None:
        return None
    check = await run_check(request, db, *credentials)
    if check.ok or check.reason == "missing_permissions":
        take_verdict(request, check)
        return check
    return None


async def _folder(client: BambuddyClient, folder_id: int | None):
    """(crumbs, problem key or None, external) for the target folder, read fresh."""
    folders = await client.list_folders()
    if folder_id is not None and all(f.id != folder_id for f in folders):
        return [], "upload.folder_gone", False
    return _crumbs(folders, folder_id), None, is_external_folder(folders, folder_id)


@router.get(PATH)
async def upload_page(request: Request, folder: str | None = None, db: Session = Depends(get_db)):
    folder_id = bounded_int(folder)
    if folder is not None and folder_id is None:
        return _page(request, state="problem", status=404, error="upload.folder_gone")
    client = get_bambuddy(request)
    if client is None:
        return _page(request, state="problem", error="articles.bambuddy_missing")
    try:
        crumbs, problem, external = await _folder(client, folder_id)
    except BAMBUDDY_ERRORS:
        return _page(request, state="problem", error="articles.bambuddy_error")
    if problem:
        return _page(request, state="problem", status=404, error=problem)
    if external:
        return _page(request, state="external", folder_id=folder_id, crumbs=crumbs)
    check = await _rights(request, db)
    if check is None:
        return _page(request, state="problem", folder_id=folder_id, crumbs=crumbs, error="articles.bambuddy_error")
    if not check.can_upload:
        return _page(request, state="no_right", folder_id=folder_id, crumbs=crumbs)
    return _page(request, state="form", folder_id=folder_id, crumbs=crumbs)


def _refusal_key(error: BambuddyRejected) -> str:
    """Which message a refusal of Bambuddy gets; a 403 says which right is missing (Spec 5.1a)."""
    detail = error.detail or ""
    if error.status == 403:
        if "can_manage_library" in detail:
            return "upload.no_right_key"
        if "library:upload" in detail:
            return "upload.no_right_group"
        return "upload.folder_not_writable"
    if error.status == 404:
        return "upload.folder_gone"
    return "upload.rejected"


async def _find_own(client: BambuddyClient, folder_id: int | None, name: str, size: int,
                    sent_at: datetime, username: str | None) -> LibraryFile | None:
    """The uploaded file after an unclear answer, or None (Spec 5.1a).

    Like finding a queue entry again (`domain/dispatch.py`): own user, same name (the name
    was free before the send), same size, made after the send less the clock tolerance,
    and exactly one of them. Without the own user nothing can be told apart: None.
    """
    if not username:
        return None
    try:
        files = await client.list_files(folder_id)
    except BAMBUDDY_ERRORS:
        return None
    key = name_key(name)
    found = [
        f for f in files
        if name_key(f.filename) == key
        and f.created_by == username
        and f.size == size
        and f.created_at is not None
        and f.created_at >= sent_at - CLOCK_TOLERANCE
    ]
    return found[0] if len(found) == 1 else None


@router.post(PATH)
async def upload(request: Request, db: Session = Depends(get_db)):
    folder_raw = request.query_params.get("folder")
    folder_id = bounded_int(folder_raw)
    # Before anything reads the body: no `Transfer-Encoding`, a length within the limit
    # (`web/bodylimit.py`; the middleware counts what is read).
    if not upload_length_ok(request, BODY_LIMIT):
        return _page(request, state="form", status=413, folder_id=folder_id, error="upload.error.too_large")
    form = await request.form(max_files=1, max_fields=4)
    await verify_csrf(request)  # reads the same, already parsed form
    upload_file = form.get(FIELD)
    try:
        return await _upload(request, db, folder_raw, folder_id, upload_file)
    finally:
        if isinstance(upload_file, UploadFile):
            await upload_file.close()  # Starlette's temporary file is gone with it


async def _upload(request: Request, db: Session, folder_raw: str | None, folder_id: int | None, upload_file):
    if folder_raw is not None and folder_id is None:
        return _page(request, state="problem", status=404, error="upload.folder_gone")
    client = get_bambuddy(request)
    if client is None:
        return _page(request, state="problem", error="articles.bambuddy_missing")
    try:
        crumbs, problem, external = await _folder(client, folder_id)
    except BAMBUDDY_ERRORS:
        return _page(request, state="problem", folder_id=folder_id, error="articles.bambuddy_error")
    if problem:
        return _page(request, state="problem", status=404, error=problem)
    if external:
        return _page(request, state="external", status=409, folder_id=folder_id, crumbs=crumbs)
    check = await _rights(request, db)
    if check is None:
        return _page(request, state="problem", folder_id=folder_id, crumbs=crumbs, error="articles.bambuddy_error")
    if not check.can_upload:
        return _page(request, state="no_right", folder_id=folder_id, crumbs=crumbs)

    def again(error: str, status: int = 400, **params: object):
        return _page(request, state="form", status=status, folder_id=folder_id, crumbs=crumbs, error=error, **params)

    if not isinstance(upload_file, UploadFile) or not upload_file.filename:
        return again("upload.error.no_file")
    try:
        name = upload_name(upload_file.filename)
        await run_in_threadpool(check_sliced_3mf, upload_file.file)
    except PrintFileError as error:
        return again(f"upload.error.{error.code}", 413 if error.code == "too_large" else 400)
    size = await run_in_threadpool(lambda: upload_file.file.seek(0, 2))
    username = st.get_setting(db, "bambuddy_username")
    release_connection(db)  # nothing is pending; no connection is held during the long send

    async with request.app.state.upload_lock:
        try:
            same = [f for f in await client.list_files(folder_id) if name_key(f.filename) == name_key(name)]
        except BAMBUDDY_ERRORS:
            return again("articles.bambuddy_error", 502)
        if same:
            existing = same[0]
            key = "upload.same_name" if existing.sliced else "upload.same_name_unsliced"
            return again(key, 409, name=existing.filename, existing_id=existing.id if existing.sliced else None)
        sent_at = _now()
        try:
            result = await client.upload_file(folder_id, name, upload_file.file)
        except BambuddyRejected as error:
            key = _refusal_key(error)
            return again(key, 409 if key != "upload.rejected" else 400, detail=(error.detail or "")[:MAX_DETAIL])
        except BambuddyUnavailable:
            found = await _find_own(client, folder_id, name, size, sent_at, username if isinstance(username, str) else None)
            if found is None:
                log.warning("upload: answer unclear and the file was not found")
                return again("upload.unclear", 502)
            flash(request, "upload.found_after_unclear")
            if not found.sliced:
                return again("upload.bambuddy_not_sliced", 409, name=found.filename)
            return RedirectResponse(f"/articles/new/file/{found.id}", status_code=303)

    if not result.sliced:
        return again("upload.bambuddy_not_sliced", 409, name=result.filename)
    if result.duplicate_of is not None:
        try:
            twin = await client.get_file(result.duplicate_of)
        except BAMBUDDY_ERRORS:
            twin = None
        if twin is not None:
            flash(request, "upload.duplicate", name=twin.filename)
    return RedirectResponse(f"/articles/new/file/{result.id}", status_code=303)
