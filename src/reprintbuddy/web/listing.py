"""Tools for the overview pages: search, status filters, tiles or list (Spec 10.1).

The choice of tiles or list is kept per page in a cookie `rb_layout_<page>`. The links set it with
`?layout=list|tiles` and answer with a redirect to the same address without the parameter, so it
works without JavaScript and the page does not flicker. Without a choice the page holds both
layouts and the style sheet shows tiles on narrow screens and the list on wide ones.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from reprintbuddy.models import Article

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


@dataclass(frozen=True)
class ListPage:
    shown: list  # the rows that pass the search and the status filter
    status: str | None  # the chosen status, None for all
    context: dict[str, object]  # total, kpis, filters, layout, layout_links, q, search_keep


def list_page(
    request: Request,
    *,
    page: str,
    rows: Sequence,
    states: Sequence[str],
    status_of: Callable[[object], str],
    search_texts: Callable[[object], Iterable[str | None]],
    q: str | None,
    status_filter: bool,
    view: str | None,
    cards: list,
) -> ListPage:
    """The step the overview pages of parts, sets, articles and orders share (Spec 10.1; 0.4.7, R8):
    the cards (built by the page) and the total count the whole view, the filter counts follow the
    search, the list shows what passes both. `status_filter` False (an archive) ignores `?status=`.
    `view` is the view a new search keeps (archived, closed) or None for the default view."""
    status = pick(request.query_params.get("status"), states) if status_filter else None
    found = [row for row in rows if matches(q, *search_texts(row))]
    counts = Counter(status_of(row) for row in found)
    keep = (("status", status), ("view", view))
    layout = layout_of(request, page)
    return ListPage(
        shown=[row for row in found if status is None or status_of(row) == status],
        status=status,
        context={
            "total": len(rows),
            "kpis": cards,
            "filters": filter_links(request, states, counts, len(found), status),
            "layout": layout,
            "layout_links": layout_links(request, layout),
            "q": q,
            "search_keep": [(name, value) for name, value in keep if value],  # a new search keeps these
        },
    )


# ---- the filter by article of the work lists (Spec 8.2; the later QR code of an article opens
# the list with `?article=<id>`) -----------------------------------------------------------------

# An id in an address or a form: ASCII digits only, at most 18 of them (fits every id).
_ARTICLE_ID = re.compile(r"[0-9]{1,18}", re.ASCII)


def article_id_of(raw: object) -> int | None:
    """The id in a `article` parameter or field; anything that is no id counts as no filter."""
    text = str(raw) if raw is not None else ""
    return int(text) if _ARTICLE_ID.fullmatch(text) else None


@dataclass(frozen=True)
class ArticleFilter:
    id: int
    name: str
    clear_href: str  # the list without the filter


def article_filter(db: Session, raw: object, path: str) -> ArticleFilter | None:
    """The filter of `raw` (an `article` value) on the list at `path`; `None` for no filter.

    An id that is no article is no filter either: the whole list shows instead of an empty one
    that names nothing.
    """
    article_id = article_id_of(raw)
    if article_id is None:
        return None
    article = db.get(Article, article_id)
    return ArticleFilter(article.id, article.name, path) if article is not None else None


def with_article(path: str, article_id: int | None) -> str:
    """`path` with `?article=<id>` when there is a filter (the redirect after an action)."""
    return f"{path}?article={article_id}" if article_id is not None else path
