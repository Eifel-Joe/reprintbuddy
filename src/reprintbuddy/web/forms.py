"""Helpers for reading forms: strict number parsing and readable error texts."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse

from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.web.flash import flash
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction
from reprintbuddy.web.i18n import translate

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
    work: Callable[[Any], dict[str, object]],
    success_url: str | Callable[[], str],
    flash_key: str,
    on_error: Callable[[str, int, Exception], Any],
):
    """A one-time form with one action (archive, restore, ...): `work(tx)` runs in the write
    transaction and returns the parameters of the success message. A second submission of
    the same form is reported as such; a rejected action shows `on_error(text, status, exc)`
    (which may be a coroutine). `success_url` may be a function: it is called once `work` is
    done (or was skipped as a repeat), for an address that depends on what the work found."""
    from reprintbuddy.web.deps import current_language

    form = await request.form()
    try:
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            params = work(tx)
    except FormTokenUsed:
        return duplicate_redirect(request, success_url() if callable(success_url) else success_url)
    except (DomainError, FormTokenInvalid) as exc:
        result = on_error(error_text(current_language(request), exc), error_status(exc), exc)
        return await result if inspect.isawaitable(result) else result
    return saved_redirect(request, success_url() if callable(success_url) else success_url, flash_key, **params)
