"""Helpers for reading forms: strict number parsing and readable error texts."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse

from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formatting import format_date, to_local
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.i18n import translate

MAX_ID = 2**31 - 1  # the largest record number a form may name; was written out in four route modules


def maintenance_redirect(code: str | None, anchor: str) -> RedirectResponse:
    """Back to the maintenance tab of the settings, at `anchor`, with the message `code` if any."""
    query = f"&ops={code}" if code else ""
    return RedirectResponse(f"/settings?tab=maintenance{query}#{anchor}", status_code=303)


_INT = re.compile(r"-?[0-9]{1,18}", re.ASCII)
_BOUNDED = re.compile(r"[0-9]{1,18}", re.ASCII)


def bounded_int(raw: str | None) -> int | None:
    """A non-negative whole number of at most 18 ASCII digits, else None.

    No `str.isdigit` (it accepts characters like the superscript two, which `int` then rejects)
    and no unbounded `int()` (Python raises ValueError above 4300 digits, SQLite cannot store
    2**63 or more). Same bound as `parse_int`.
    """
    if raw is None or not _BOUNDED.fullmatch(raw):
        return None
    return int(raw)


def parse_int(
    raw: str | None, *, field: str, min: int, max: int, default: int | None = None
) -> int:
    """A whole number from a form field. Only digits (and a leading minus) are accepted."""
    text = (raw or "").strip()
    if not text:
        if default is not None:
            return default
        raise ValidationError("form.required", field=field)
    if not _INT.fullmatch(text):
        raise ValidationError("form.invalid_number", field=field, min=min, max=max)
    value = int(text)
    if not min <= value <= max:
        raise ValidationError("form.invalid_number", field=field, min=min, max=max)
    return value


def parse_text(
    raw: str | None, *, field: str, max_len: int, required: bool = False
) -> str | None:
    text = (raw or "").strip()
    if not text:
        if required:
            raise ValidationError("form.required", field=field)
        return None
    if len(text) > max_len:
        raise ValidationError("form.too_long", field=field, max=max_len)
    return text


def parse_bool(raw: str | None) -> bool:
    """A checkbox: present with a value like "on" means true; absent means false."""
    return (raw or "").strip().lower() in ("on", "1", "true", "yes")


def error_text(lang: str, err: DomainError | FormTokenInvalid) -> str:
    """The message of a domain error; a parameter `field` is itself a translation key."""
    if isinstance(err, FormTokenInvalid):
        return translate(lang, "form.expired")
    params = dict(err.params)
    if isinstance(params.get("field"), str):
        params["field"] = translate(lang, params["field"])
    for name, value in params.items():
        if isinstance(value, datetime):  # naive UTC from the domain: shown as the local day (p210)
            params[name] = format_date(to_local(value).date(), lang)
        elif isinstance(value, list):  # (name, archived) pairs: an archived one is marked (0.4.3, condition A1)
            mark = translate(lang, "sets.archived_mark")
            params[name] = ", ".join(f"{n} {mark}" if archived else n for n, archived in value)
    text = translate(lang, err.key, **params)
    if isinstance(params.get("supplier"), int):  # an error in one of several suppliers of a part (Spec 5.3)
        return translate(lang, "parts.supplier_error", n=params["supplier"], error=text)
    return text


def error_status(err: object) -> int:
    """400 for an expired or forged form, 200 (the form is shown again) for a rejected input."""
    return 400 if isinstance(err, FormTokenInvalid) else 200


def duplicate_redirect(request: Request, url: str) -> RedirectResponse:
    """The second submission of a form: nothing is booked again, the user is told so."""
    flash(request, "form.duplicate")
    return RedirectResponse(url, status_code=303)


def saved_redirect(request: Request, url: str, key: str, **params: object) -> RedirectResponse:
    flash(request, key, **params)
    return RedirectResponse(url, status_code=303)


async def run_form_action(
    request: Request,
    *,
    work: Callable[..., Any],
    on_error: Callable[[str, int, Exception], Any],
    success_url: str | Callable[[], str] | None = None,
    flash_key: str | None = None,
    check: Callable[[], Any] | None = None,
    once: bool = True,
    done: Callable[[Any], Any] | None = None,
    duplicate_url: str | None = None,
):
    """Save a form once, however often it is sent (the double-submit guard of every form that books
    or creates something).

    `work(tx)` runs in the write transaction; with `check` it is `work(tx, check())`: `check` reads
    and checks the input *before* the transaction, which uses the one-time token up first thing, so
    a rejected input is shown even when the form was sent twice. `once=False` saves without a token
    (editing a saved record). A rejected input or action shows `on_error(text, status, exc)`: status
    200 to show the form again, 400 for an expired or forged form.

    The answer to a success is `done(result)` with what `work` returned, or else the message
    `flash_key` (with `work`'s result as its parameters) and a redirect to `success_url`. A form sent
    again goes to `duplicate_url`, or else to `success_url`, with "already saved". `success_url` may
    be a function, called once `work` is done (or was skipped as a repeat). `on_error` and `done` may
    be coroutines.

    0.4.8 (R9): the forms that show their input again run through here too. Not here, on purpose:
    the forms that only use their token up before a call to the outside (sample label, article label,
    test message, the switch of the update check, "poll now"); they answer a repeat and an expired
    form with a message on the same page. Tests: tests/web/test_run_form_action.py.
    """
    from reprintbuddy.web.deps import current_language

    if done is None and (success_url is None or flash_key is None):
        raise TypeError("run_form_action needs `done`, or `success_url` and `flash_key`")
    form = await request.form()
    try:
        checked = check() if check is not None else None
        transaction = form_transaction(request, str(form.get("form_token", ""))) if once else write_transaction(request)
        with transaction as tx:
            result = work(tx) if check is None else work(tx, checked)
    except FormTokenUsed:
        target = duplicate_url if duplicate_url is not None else success_url
        return duplicate_redirect(request, target() if callable(target) else target)
    except (DomainError, FormTokenInvalid) as exc:
        return await _answer(on_error(error_text(current_language(request), exc), error_status(exc), exc))
    if done is not None:
        return await _answer(done(result))
    return saved_redirect(request, success_url() if callable(success_url) else success_url, flash_key, **result)


async def _answer(result: Any) -> Any:
    return await result if inspect.isawaitable(result) else result
