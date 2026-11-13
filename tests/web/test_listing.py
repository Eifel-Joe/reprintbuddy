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


def test_url_with_drops_every_repeated_key_and_appends_the_new_value_once():
    request = make_request("/articles", "layout=grid&q=a&layout=tiles")
    assert listing.url_with(request, layout=None) == "/articles?q=a"
    assert listing.url_with(request, layout="list") == "/articles?q=a&layout=list"
    request = make_request("/articles", "status=a&status=b")
    assert listing.url_with(request, status=None) == "/articles"


def test_remember_layout_with_repeated_parameter_redirects_cleanly():
    response = listing.remember_layout(make_request(query="layout=grid&layout=tiles"), "articles")
    assert response.headers["location"] == "/articles"


def test_url_with_never_gives_a_protocol_relative_path():
    assert listing.url_with(make_request("//evil.example", "q=a"), q="b") == "/evil.example?q=b"
    assert listing.url_with(make_request("//evil.example"), q=None) == "/evil.example"


def test_the_app_offers_fill_percent_to_templates(client):
    env = client.app.state.templates.env
    assert env.from_string("{{ fill_percent(3, 6) }}|{{ fill_percent(3, 0) }}").render() == "50|None"


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
    assert links[1].href == "/articles?q=a&status=low"
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
        '{{ ui.search_form(q, "ph", [("view", "a<b")]) }}'
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
    assert '<input type="hidden" name="view" value="a&lt;b">' in html
    assert '<nav class="filters"' in html
    assert 'class="filter active"' in html and 'list.filter_all' in html
    assert 'status.article.low' in html and '<span class="count">2</span>' in html
    assert '<nav class="layout-toggle"' in html
    assert html.count('aria-current="true"') >= 2  # active filter and the chosen layout
    assert 'list.layout_tiles' in html and 'list.layout_list' in html
    assert html.count('class="bar ') == 1 and 'width: 50%' in html
    assert '<div class="collection layout-tiles">X</div>' in html


def test_list_page_filters_searches_and_keeps():
    """0.4.7 (R8): the one step the four overview pages share. The cards and the total count the whole
    view, the filter counts follow the search, the rows pass search and filter, a new search keeps the
    filter and the view."""
    from types import SimpleNamespace

    rows = [SimpleNamespace(name=n, state=s) for n, s in (("Alpha", "a"), ("Beta", "b"), ("Alpine", "a"))]
    request = make_request("/parts", "status=a&view=archived&q=Al", cookies={"rb_layout_parts": "list"})
    page = listing.list_page(
        request, page="parts", rows=rows, states=("a", "b"), status_of=lambda r: r.state,
        search_texts=lambda r: (r.name,), q="Al", status_filter=True, view="archived", cards=["the cards"],
    )
    assert [r.name for r in page.shown] == ["Alpha", "Alpine"]
    assert page.status == "a"
    assert page.context["total"] == 3 and page.context["kpis"] == ["the cards"] and page.context["q"] == "Al"
    assert [(f.state, f.count, f.active) for f in page.context["filters"]] == [(None, 2, False), ("a", 2, True)]
    assert page.context["search_keep"] == [("status", "a"), ("view", "archived")]
    assert page.context["layout"] == "list"
    assert [link.layout for link in page.context["layout_links"]] == ["list", "tiles"]


def test_list_page_without_a_status_filter_ignores_the_parameter():
    from types import SimpleNamespace

    rows = [SimpleNamespace(name="Alpha", state="a")]
    page = listing.list_page(
        make_request("/parts", "status=a"), page="parts", rows=rows, states=("a",), status_of=lambda r: r.state,
        search_texts=lambda r: (r.name,), q=None, status_filter=False, view=None, cards=[],
    )
    assert (page.status, len(page.shown), page.context["search_keep"]) == (None, 1, [])
