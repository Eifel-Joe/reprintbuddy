"""Archive and restore of parts, sets and articles: the two POST routes each list has (Spec 5.5).

Parts, sets and articles each had their own handler factory with the same steps (0.4.7, R6): load
the record or answer 404, run the domain action in a one-time form transaction, say so with the
record's name, and on a refusal show the page the button was on with the reason. What differs is
passed in: where to go after it and which page shows a refusal. The addresses, methods and CSRF
check stay as they were (tests/web/test_archive_routes_kept.py).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import get_db
from reprintbuddy.web.forms import run_form_action

Refusal = Callable[[Request, Session, object, str, int, Exception], Response]


def add_archive_routes(
    router: APIRouter,
    *,
    base: str,
    param: str,
    load: Callable[[Session, int], object],
    archive: Callable[[Session, int], object],
    restore: Callable[[Session, int], object],
    flash_archived: str,
    flash_restored: str,
    success_url: Callable[[Request, int], Awaitable[str]],
    on_error: Refusal,
) -> None:
    """`POST {base}/{param}/archive` and `.../restore`. `load` answers 404 for an unknown record;
    `flash_archived` and `flash_restored` are the message keys, written out so that
    tests/web/test_locale_keys_used.py finds them."""

    def handler_for(action: Callable[[Session, int], object], flash_key: str):
        async def handler(request: Request, db: Session = Depends(get_db)) -> Response:
            ident = int(request.path_params[param])  # the route's `:int` has checked it
            record = load(db, ident)
            after = await success_url(request, ident)

            def work(tx) -> dict[str, object]:
                action(tx, ident)
                return {"name": record.name}

            return await run_form_action(
                request,
                work=work,
                success_url=after,
                flash_key=flash_key,
                on_error=lambda text, status, exc: on_error(request, db, record, text, status, exc),
            )

        return handler

    for verb, action, flash_key in (("archive", archive, flash_archived), ("restore", restore, flash_restored)):
        router.add_api_route(
            f"{base}/{{{param}:int}}/{verb}",
            handler_for(action, flash_key),
            methods=["POST"],
            dependencies=[Depends(verify_csrf)],
        )


def to(url: str) -> Callable[[Request, int], Awaitable[str]]:
    """A `success_url` that is always the same address (the list: an archived record leaves it)."""

    async def fixed(request: Request, ident: int) -> str:
        return url

    return fixed
