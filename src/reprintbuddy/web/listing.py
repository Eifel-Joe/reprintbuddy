"""Tools for the overview pages: search, status filters, tiles or list (Spec 10.1).

The choice of tiles or list is kept per page in a cookie `rb_layout_<page>`. The links set it with
`?layout=list|tiles` and answer with a redirect to the same address without the parameter, so it
works without JavaScript and the page does not flicker. Without a choice the page holds both
layouts and the style sheet shows tiles on narrow screens and the list on wide ones.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse

Layout = Literal["auto", "list", "tiles"]

_CHOICES = ("list", "tiles")
_COOKIE_AGE = 31536000  # one year


def _cookie_name(page: str) -> str:
    return f"rb_layout_{page}"


def layout_of(request: Request, page: str) -> Layout:
    """The remembered choice of this browser for `page`; anything unknown counts as no choice."""
    value = request.cookies.get(_cookie_name(page))
    return value if value in _CHOICES else "auto"  # type: ignore[return-value]


def url_with(request: Request, **params: str | None) -> str:
    """Path and query of the request with `params` replaced (`None` removes one). Every
    occurrence of a named key is dropped, the new value is appended once; the other parameters keep
    their order. The path always starts with exactly one `/` (never protocol-relative)."""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k not in params]
    pairs.extend((key, value) for key, value in params.items() if value is not None)
    path = "/" + quote(request.url.path, safe="/").lstrip("/")
    return f"{path}?{urlencode(pairs)}" if pairs else path


def remember_layout(request: Request, page: str) -> RedirectResponse | None:
    """If the request carries `layout`, answer with a redirect to the address without it and, for
    `list` or `tiles`, set the cookie. Otherwise `None` and the page renders as usual."""
    if "layout" not in request.query_params:
        return None
    choice = request.query_params["layout"]
    response = RedirectResponse(url_with(request, layout=None), status_code=303)
    if choice in _CHOICES:
        response.set_cookie(
            _cookie_name(page),
            choice,
            max_age=_COOKIE_AGE,
            path="/",
            samesite="lax",
            httponly=True,
            secure=request.url.scheme == "https",
        )
    return response


def matches(query: str | None, *texts: str | None) -> bool:
    """Search: empty matches everything, otherwise a case-insensitive part of one of the texts.
    The query is taken literally (`%` and `_` mean nothing special)."""
    if not query:
        return True
    needle = query.casefold()
    return any(text is not None and needle in text.casefold() for text in texts)


def pick(raw: str | None, allowed: Sequence[str]) -> str | None:
    """A parameter that must be one of `allowed`, else `None`."""
    return raw if raw in allowed else None


@dataclass(frozen=True)
class FilterLink:
    state: str | None  # None = "All"
    count: int
    href: str
    active: bool


def filter_links(
    request: Request,
    states: Sequence[str],
    counts: Mapping[str, int],
    total: int,
    current: str | None,
) -> list[FilterLink]:
    """"All" first, then every state that has entries or is the chosen one (in the order of `states`)."""
    links = [FilterLink(None, total, url_with(request, status=None), current is None)]
    for state in states:
        count = counts.get(state, 0)
        if count > 0 or state == current:
            links.append(FilterLink(state, count, url_with(request, status=state), state == current))
    return links


@dataclass(frozen=True)
class ToggleLink:
    layout: str
    href: str
    current: bool


def layout_links(request: Request, current: Layout) -> list[ToggleLink]:
    return [ToggleLink(choice, url_with(request, layout=choice), choice == current) for choice in _CHOICES]
