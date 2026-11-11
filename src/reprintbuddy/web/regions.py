"""Named regions of a page, fetched again by the page itself (Spec 10.5.3).

A page marks parts of itself with the `region` macro of `_ui.html`. To bring one up to date,
the browser asks for the page's own address with the header `X-Live-Regions: list summary`.
The server renders the whole page with the same template as always and cuts out the pieces
between `<!--live:NAME-->` and `<!--/live:NAME-->`: no second way of drawing a page, and the
gate, the sign-in and the error pages hold as for any other request.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from starlette.requests import Request

HEADER = "X-Live-Regions"
MAX = 8  # more regions than any page has; a longer list is cut, not an error
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


def requested(request: Request) -> list[str]:
    """The region names a GET request asks for: valid names only, each once, at most MAX.

    Any other method gets the whole page: a page shown as the answer to a POST has an address
    that only answers POST, so it never asks for regions.
    """
    if request.method != "GET":
        return []
    names: list[str] = []
    for word in request.headers.get(HEADER, "").split():
        if NAME.fullmatch(word) and word not in names:
            names.append(word)
            if len(names) == MAX:
                break
    return names


# Set by live.js right before it loads a page again by itself (new version, a failed refetch, signed
# out); the next page clears it. Such a load is no opening (Spec 10.2, decided 11.11.2026).
AUTO_RELOAD_COOKIE = "rb_auto_reload"


def page_load(request: Request) -> bool:
    """Opening a page for "new" on the overview (Spec 10.2, 0.4.1): a GET of the whole page by you,
    not the live refetch of its regions and not a reload the live script does by itself."""
    return (
        request.method == "GET"
        and not requested(request)
        and request.cookies.get(AUTO_RELOAD_COOKIE) != "1"
    )


def extract(html: str, names: Sequence[str]) -> dict[str, str]:
    """The text between the markers of each name found, in the order of `names`."""
    found: dict[str, str] = {}
    for name in names:
        start_mark, end_mark = f"<!--live:{name}-->", f"<!--/live:{name}-->"
        start = html.find(start_mark)
        if start < 0:
            continue
        start += len(start_mark)
        end = html.find(end_mark, start)
        if end >= 0:
            found[name] = html[start:end]
    return found
