"""Search, filters and the remembered tiles or list view of the overview pages (Spec 10.1)."""

from pathlib import Path

from jinja2 import DictLoader, Environment, FileSystemLoader
from starlette.requests import Request

from reprintbuddy.web import formatting, listing
from reprintbuddy.web.listing import FilterLink

TEMPLATES = Path(listing.__file__).parent / "templates"


def make_request(path="/articles", query="", cookies=None, scheme="http"):
    headers = [(b"host", b"testserver")]
    if cookies:
        value = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers.append((b"cookie", value.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": headers,
        "server": ("testserver", 80),
    }
    return Request(scope)


def test_layout_of_reads_only_known_values():
    assert listing.layout_of(make_request(cookies={"rb_layout_articles": "tiles"}), "articles") == "tiles"
    assert listing.layout_of(make_request(cookies={"rb_layout_articles": "list"}), "articles") == "list"
    assert listing.layout_of(make_request(cookies={"rb_layout_articles": "grid"}), "articles") == "auto"
    assert listing.layout_of(make_request(), "articles") == "auto"
    assert listing.layout_of(make_request(cookies={"rb_layout_parts": "tiles"}), "articles") == "auto"


def test_remember_layout_sets_cookie_and_drops_the_parameter():
    response = listing.remember_layout(make_request(query="q=a&layout=tiles"), "articles")
    assert response.status_code == 303
    assert response.headers["location"] == "/articles?q=a"
    cookie = response.headers["set-cookie"]
    assert "rb_layout_articles=tiles" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=31536000" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie


def test_remember_layout_is_secure_on_https():
    response = listing.remember_layout(make_request(query="layout=list", scheme="https"), "articles")
    assert "Secure" in response.headers["set-cookie"]
    assert response.headers["location"] == "/articles"


def test_remember_layout_ignores_garbage():
    response = listing.remember_layout(make_request(query="layout=grid"), "articles")
    assert response.status_code == 303
    assert response.headers["location"] == "/articles"
    assert "set-cookie" not in response.headers
    assert listing.remember_layout(make_request(query="q=a"), "articles") is None


def test_url_with_replaces_and_drops():
    request = make_request("/parts", "view=archived&status=ok")
    assert listing.url_with(request, status=None, q="x") == "/parts?view=archived&q=x"
    assert listing.url_with(request, status=None, view=None) == "/parts"
    assert listing.url_with(request, status="low") == "/parts?view=archived&status=low"


def test_url_with_encodes_values():
    assert listing.url_with(make_request("/parts"), q="a b&c") == "/parts?q=a+b%26c"


def test_matches_is_case_insensitive_and_literal():
    assert listing.matches("a-1", "Template", "A-100") is True
    assert listing.matches("%", "Template") is False
    assert listing.matches("_", "Template") is False
    assert listing.matches(None, "x") is True
    assert listing.matches("", "x") is True
    assert listing.matches("x", None, "y") is False


def test_pick_allows_only_listed_values():
    assert listing.pick("low", ("low", "ok")) == "low"
    assert listing.pick("nope", ("low", "ok")) is None
    assert listing.pick(None, ("low", "ok")) is None


def test_filter_links_all_first_and_only_present_states():
    request = make_request("/articles", "status=low&q=a")
    states = ("overbooked", "below_min", "low", "ok")
    links = listing.filter_links(request, states, {"low": 2, "ok": 1}, 3, None)
    assert [(link.state, link.count, link.active) for link in links] == [
        (None, 3, True),
        ("low", 2, False),
        ("ok", 1, False),
    ]
    assert links[0].href == "/articles?q=a"
    assert links[1].href == "/articles?status=low&q=a"
    links = listing.filter_links(request, states, {"low": 2, "ok": 1}, 3, "below_min")
    assert links[0] == FilterLink(None, 3, "/articles?q=a", False)
    assert (links[1].state, links[1].count, links[1].active) == ("below_min", 0, True)


def test_layout_links():
    links = listing.layout_links(make_request("/parts", "q=a"), "tiles")
    assert [(link.layout, link.current) for link in links] == [("list", False), ("tiles", True)]
    assert links[0].href == "/parts?q=a&layout=list"


def test_fill_percent():
    assert formatting.fill_percent(3, 0) is None
    assert formatting.fill_percent(-2, 6) == 0
    assert formatting.fill_percent(3, 6) == 50
    assert formatting.fill_percent(9, 6) == 100


def _render(source, **context):
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)
    env.globals.update(
        t=lambda key, **kw: key,
        tc=lambda key, n, **kw: f"{key}:{n}",
        status_tone=lambda state: "ok",
        app_version="9.9.9",
    )
    return env.from_string(source).render(lang="en", **context)


def test_macros_render_the_markup_the_pages_rely_on():
    html = _render(
        '{% import "_ui.html" as ui with context %}'
        '{{ ui.search_form(q, "ph", [("view", "archived")]) }}'
        '{{ ui.filter_bar("article", links) }}'
        '{{ ui.layout_toggle(toggles) }}'
        '{{ ui.bar(50, "low") }}{{ ui.bar(None, "low") }}'
        '{% call ui.collection("tiles") %}X{% endcall %}',
        q="a<b",
        links=[FilterLink(None, 3, "/a", True), FilterLink("low", 2, "/a?status=low", False)],
        toggles=listing.layout_links(make_request("/a"), "list"),
    )
    assert '<form class="search" method="get" role="search">' in html
    assert 'name="q"' in html and 'value="a&lt;b"' in html
    assert '<input type="hidden" name="view" value="archived">' in html
    assert '<nav class="filters"' in html
    assert 'class="filter active"' in html and 'list.filter_all' in html
    assert 'status.article.low' in html and '<span class="count">2</span>' in html
    assert '<nav class="layout-toggle"' in html
    assert html.count('aria-current="true"') >= 2  # active filter and the chosen layout
    assert 'list.layout_tiles' in html and 'list.layout_list' in html
    assert html.count('class="bar ') == 1 and 'width: 50%' in html
    assert '<div class="collection layout-tiles">X</div>' in html
