"""The overview page (Spec 10.2 since 0.4.2): head line, warnings, four tiles, the print plan.

No lists per article: each tile shows numbers and a small picture and leads to its pages. `test_overview_view.py`
checks what the tiles make of the counts, `test_overview_head.py` the head line, `test_overview_plan.py` the
print plan; this file checks that the page counts with the functions of the pages behind the tiles and that
every jump shows what it counts (p420).
"""

import re
from datetime import date

from markupsafe import escape

from conftest import sm_of
from factories import make_article, make_assembled, make_dispatch, make_entry, make_part
from texts import en_html as en
from reprintbuddy import settings as st
from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem, AssembledItem
from reprintbuddy.domain.reorder import Reach
from reprintbuddy.i18n import translate_count
from reprintbuddy.models import Inspection
from reprintbuddy.web import regions


def enc(key, n):
    return str(escape(translate_count("en", key, n)))


def region(page, name):
    found = re.search(rf"<!--live:{name}-->(.*?)<!--/live:{name}-->", page, re.S)
    assert found, f"no region {name}"
    return found.group(1)


def tile(page, name):
    found = re.search(rf'<section class="card ov-tile" data-tile="{name}">(.*?)</section>', page, re.S)
    assert found, f"no tile {name}"
    return found.group(1)


def text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def sub_links(part):
    """The links inside a tile's small line: [(href, text)]."""
    return [(href, text(inner)) for href, inner in re.findall(r'<a class="ov-sub[^"]*" href="([^"]+)">(.*?)</a>', part, re.S)]


def number(part):
    found = re.search(r'<span class="ov-number tone-([a-z]+)">([^<]*)</span>', part)
    assert found, "no number"
    value = found.group(2)
    return found.group(1), int(value) if value.isdigit() else value


def todo(part):
    """The lines of the tile "to do": [(href, label, tone, count, note)]."""
    return [
        (href, label, tone, int(count), text(note))
        for href, label, tone, count, note in re.findall(
            r'<a class="ov-line" href="([^"]+)"><span class="ov-line-label">([^<]*)</span>'
            r'<span class="ov-number tone-([a-z]+)">(\d+)</span><span class="ov-line-note">(.*?)</span></a>',
            part, re.S,
        )
    ]


def st_now():
    from reprintbuddy.models import utcnow

    return utcnow()


def seed(client):
    """Something on every tile except filament (its stock is not known here)."""
    with sm_of(client).begin() as s:
        ok = make_article(s, name="Bracket", sku="BR-1", stock=8, reserved=0, min_stock=4, buffer=2)
        low = make_article(s, name="Clip", stock=1, min_stock=3, buffer=1, units_per_print=2)
        over = make_article(s, name="Hook", stock=0, reserved=2, min_stock=1, buffer=1, awaiting_assembly=1,
                            paused=True, pause_reason="two_failures")
        make_article(s, name="Dowel", stock=0, min_stock=2)  # nothing planned: below the minimum
        make_article(s, name="Old", stock=0, min_stock=5, archived_at=st_now())
        make_entry(s, low.id, prints=2)
        make_entry(s, ok.id, prints=1, held=True)
        make_dispatch(s, low.id, status="printing", units=2)
        make_dispatch(s, over.id, status="queued", units=1)
        done = make_dispatch(s, ok.id, status="completed", units=3)
        s.add(Inspection(dispatch_id=done.id, article_id=ok.id, units=3))  # a print waiting for the check
        make_dispatch(s, ok.id, status="failed", units=5)
        make_part(s, name="Insert", stock=0, min_stock=10)  # a part below its minimum: one suggestion
        # Assembly: built on order, 2 ordered; a variant on stock below its target is no assembly due.
        segment = make_article(s, name="Segment")
        hose = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(segment.id, 2)])
        make_assembled(s, name="Box", sku="EX-B1", mode="stock", base=[(segment.id, 1)], min_stock=2, buffer=1)
        shipped_soon = orders.create_order(
            s, reference="R-1", order_date=date(2026, 11, 11), items=[ArticleItem(ok.id, 1)], actor="user")
        st.set_setting(s, "orders_seen_id", shipped_soon.id, commit=False)  # R-1 seen, R-2 new
        orders.create_order(
            s, reference="R-2", order_date=date(2026, 11, 11), items=[AssembledItem(hose.id, 2)], actor="user")


def test_four_tiles_in_the_order_of_the_spec(ready_client):
    part = region(ready_client.get("/").text, "tiles")
    assert re.findall(r'data-tile="([a-z]+)"', part) == ["orders", "stock", "filament", "todo"]


def test_tiles_count_what_is_due(ready_client):
    from reprintbuddy.domain import assembled

    seed(ready_client)
    with sm_of(ready_client).begin() as s:
        assert assembled.list_buildable(s)  # the stock variant waits there, and still does not count
    page = ready_client.get("/").text
    orders_tile = tile(page, "orders")
    assert number(orders_tile) == ("warn", 2)
    assert sub_links(orders_tile) == [
        ("/orders?status=ready", enc("overview.tile.orders_ready", 1)),
        ("/orders?status=open", enc("overview.tile.orders_waiting", 1)),
    ]
    assert en("overview.tile.new", n=1) in orders_tile
    stock = tile(page, "stock")
    # enough: Bracket; low: Clip (its reprint runs); below: Dowel, the box on stock; overbooked: Hook, Segment
    assert enc("overview.tile.stock", 6) in stock
    assert sub_links(stock) == [
        ("/articles?status=ok", enc("overview.tile.stock_ok", 1)),
        ("/articles?status=low", enc("overview.tile.stock_low", 1)),
        ("/articles?status=below_min", enc("overview.tile.stock_below_min", 2)),
        ("/articles?status=overbooked", enc("overview.tile.stock_overbooked", 2)),
    ]
    assert todo(tile(page, "todo")) == [
        ("/inspection", en("overview.tile.inspection"), "warn", 1, enc("overview.tile.pieces", 3)),
        ("/assembly", en("overview.tile.assembly"), "warn", 1 + 2, ""),
        ("/reorder", en("overview.tile.reorder"), "warn", 1, ""),
    ]


def _ids_on(page, kind):
    return set(re.findall(rf'href="/{kind}/(\d+)"', page))


def test_every_stock_jump_shows_what_it_counts(ready_client):
    """p420: the tile counts per article like the articles page, so each filter lists as many rows."""
    seed(ready_client)
    stock = tile(ready_client.get("/").text, "stock")
    links = sub_links(stock)
    assert len(links) == 4
    for href, label in links:
        shown = len(_ids_on(region(ready_client.get(href).text, "list"), "articles"))
        assert label.startswith(f"{shown} "), (href, label, shown)


def test_low_articles_are_shown(ready_client):
    """p421: an article below its minimum whose reprint is running stays yellow ("low") and is shown."""
    with sm_of(ready_client).begin() as s:
        clip = make_article(s, name="Clip", stock=1, min_stock=3, buffer=1, units_per_print=2)
        make_entry(s, clip.id, prints=2)
    stock = tile(ready_client.get("/").text, "stock")
    assert sub_links(stock) == [("/articles?status=low", enc("overview.tile.stock_low", 1))]
    assert 'class="tone-warn"' in stock  # its part of the bar


def test_order_jumps_show_what_they_count(ready_client):
    seed(ready_client)
    for href, label in sub_links(tile(ready_client.get("/").text, "orders")):
        shown = len(_ids_on(region(ready_client.get(href).text, "list"), "orders"))
        assert label.startswith(f"{shown} "), (href, label, shown)


def test_tiles_and_plan_follow_what_they_show(ready_client):
    page = ready_client.get("/").text
    assert ('data-live-region="tiles" '
            'data-live-topics="article order queue inspection bambuddy filament reorder part journal settings"' in page)
    assert 'data-live-region="plan" data-live-topics="printers bambuddy queue article settings live-channel"' in page
    assert 'data-live-region="summary"' not in page
    only = ready_client.get("/", headers={regions.HEADER: "tiles"})
    assert 'class="ov-tiles"' in only.text


def test_no_lists_and_no_numbers_per_article(ready_client):
    seed(ready_client)
    page = ready_client.get("/").text
    assert "<table" not in page
    assert "Clip" not in page and "Bracket" not in page


def _filament(monkeypatch, short, rows):
    from reprintbuddy.domain import reorder

    found = [type("S", (), {"item_type": "filament", "item_id": i})() for i in range(short or 0)]
    monkeypatch.setattr(reorder, "current", lambda *a, **k: reorder.Current(None if short is None else {}, None, found))
    monkeypatch.setattr(reorder, "filament_reach", lambda *a, **k: rows)


def test_filament_tile_lists_three_shortest_and_the_young_hint(ready_client, monkeypatch):
    rows = [Reach(1, "PETG black", 5, True, True), Reach(2, "PLA white", 23, False, False),
            Reach(3, "PLA red", 40, False, False), Reach(4, "PLA blue", None, False, False)]
    _filament(monkeypatch, 1, rows)
    part = tile(ready_client.get("/").text, "filament")
    assert number(part) == ("warn", 1)
    found = [(tone, text(label), text(value)) for tone, label, value in re.findall(
        r'<li class="ov-row tone-([a-z]+)"><span>(.*?)</span><span>(.*?)</span></li>', part, re.S)]
    assert found == [
        ("warn", "PETG black", enc("overview.tile.lasts", 5)),
        ("ok", "PLA white", enc("overview.tile.lasts", 23)),
        ("ok", "PLA red", enc("overview.tile.lasts", 40)),
    ]
    assert en("overview.tile.young") in part
    _filament(monkeypatch, 0, rows[1:3])
    part = tile(ready_client.get("/").text, "filament")
    assert number(part) == ("neutral", 0) and en("overview.tile.young") not in part


def test_filament_stock_unknown(ready_client, monkeypatch):
    _filament(monkeypatch, None, [])
    part = tile(ready_client.get("/").text, "filament")
    assert number(part) == ("neutral", en("overview.tile.stock_unknown"))
    assert "ov-row" not in part


def test_new_suggestions_since_the_reorder_page_was_loaded(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Insert", stock=0, min_stock=10)
    ready_client.get("/reorder")
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Nut", stock=0, min_stock=10)
    line = todo(tile(ready_client.get("/").text, "todo"))[2]
    assert line[3:] == (2, en("overview.tile.new", n=1))
    ready_client.get("/")  # looking at the overview clears nothing
    assert todo(tile(ready_client.get("/").text, "todo"))[2][4] == en("overview.tile.new", n=1)


def test_empty_database(ready_client):
    page = ready_client.get("/").text
    assert number(tile(page, "orders")) == ("neutral", 0)
    assert sub_links(tile(page, "orders")) == [] and sub_links(tile(page, "stock")) == []
    assert [line[2:4] for line in todo(tile(page, "todo"))] == [("neutral", 0)] * 3
    assert en("overview.no_articles") in page
    assert 'href="/articles/new"' in page


def test_only_archived_articles_count_as_empty(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Old", archived_at=st_now())
    assert en("overview.no_articles") in ready_client.get("/").text


def test_with_an_article_no_empty_start(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="New")
    assert en("overview.no_articles") not in ready_client.get("/").text


def test_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get("/").text
    for word in ("Bestellungen", "Lager · 0 Artikel", "Filament", "Zu tun", "Zu prüfen", "Montage",
                 "Bestellvorschläge", "Druckplan"):
        assert word in page


# ---- the spools: only the tiles need them, never while Bambuddy is known to be down ------------------


def spool_calls(monkeypatch):
    from reprintbuddy.web import filament_view as route  # the overview asks through `page_spools`

    calls = []

    async def counted(request):
        calls.append(request.url.path)
        return None

    monkeypatch.setattr(route, "spools_or_none", counted)
    return calls


def test_only_the_tiles_ask_for_the_spools(ready_client, monkeypatch):
    status = ready_client.app.state.ticker.status
    status.configured, status.reachable = True, True
    calls = spool_calls(monkeypatch)
    ready_client.get("/")
    assert len(calls) == 1
    for name in ("status", "warnings", "plan", "empty"):
        ready_client.get("/", headers={regions.HEADER: name})
    assert len(calls) == 1
    ready_client.get("/", headers={regions.HEADER: "tiles"})
    assert len(calls) == 2


def test_no_spool_query_while_bambuddy_is_down_or_not_set_up(ready_client, monkeypatch):
    """The start page does not wait for a Bambuddy that is known not to answer: the count uses the
    recorded stock, as it would after the timeout."""
    status = ready_client.app.state.ticker.status
    calls = spool_calls(monkeypatch)
    status.configured, status.reachable = True, False
    seed(ready_client)
    page = ready_client.get("/").text
    status.configured, status.reachable = False, None
    ready_client.get("/")
    assert calls == []
    assert todo(tile(page, "todo"))[2][3] == 1  # the part still counts


def test_nothing_is_written_while_writes_are_paused(ready_client, monkeypatch):
    """During a restore or an update the overview counts read-only (the rule of the activity log)."""
    from reprintbuddy.domain import reorder

    seed(ready_client)
    monkeypatch.setattr(ready_client.app.state.activity, "may_write", lambda: False)
    real = reorder.current

    def read_only(*args, **kwargs):
        assert kwargs.get("write") is False, "the overview wrote while writes are paused"
        return real(*args, **kwargs)

    monkeypatch.setattr(reorder, "current", read_only)
    calls = spool_calls(monkeypatch)
    page = ready_client.get("/")
    assert page.status_code == 200
    assert calls == []
    assert todo(tile(page.text, "todo"))[2][3] == 1
