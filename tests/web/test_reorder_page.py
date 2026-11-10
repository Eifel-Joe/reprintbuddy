"""The reorder page: suggestions, "ordered" marks, "under way" and "arrived" (Spec 9.3, 9.4; B6 Task 10)."""

import re
from datetime import timedelta
from html import escape as html_escape

import pytest

from reprintbuddy import settings as st
from conftest import csrf_of, form_token_of, sm_of
from factories import assign_sort, make_article, make_filament_settings, make_part, make_snapshot, make_sort
from reprintbuddy.domain import catalog, stock
from reprintbuddy.models import ArticlePart, Part, ReorderMark, utcnow


def add_part(client, **fields):
    """Insert M4 (pack 50), needed twice per unit by Template A: a suggestion of 1 x 50."""
    values = dict(
        stock=10, pack_size=50, min_order_packs=1, lead_time_days=0, safety_value=20,
        safety_unit="qty", supplier="Example Supplier", order_url="https://shop.example/insert-m4",
    )
    values.update(fields)
    with sm_of(client).begin() as s:
        part = make_part(s, **values)
        art = make_article(s, needs_assembly=True, stock=0, min_stock=3, buffer=0)
        s.add(ArticlePart(article_id=art.id, part_id=part.id, qty_per_unit=2))
        s.flush()
        return part.id


def add_filament(client, fake):
    """A black PLA sort with 100 g and a safety cushion of 2000 g: 2 x 1000 g."""
    fake.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=100.0)
    with sm_of(client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(
            s, sort.id, pack_grams=1000, min_order_packs=1, safety_value=2000, safety_unit="grams",
            supplier="Example Supplier", order_url="https://shop.example/pla-black",
        )
        assign_sort(s, sort)  # reordered in a slot of an article: only then a suggestion (Spec 9.3)
        return sort.id


def mark_form(client, item_type, item_id, packs):
    token = form_token_of(client, "/reorder", "/reorder/mark")
    return client.post("/reorder/mark", data={
        "csrf_token": csrf_of(client, "/reorder"), "form_token": token, "item_type": item_type,
        "item_id": str(item_id), "packs": packs,
    })


def marks(client):
    with sm_of(client).begin() as s:
        return s.query(ReorderMark).all()


def test_reorder_lists_part_and_filament(bb_client, fake_bambuddy):  # Spec 9.4
    add_part(bb_client)
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    assert "Insert M4" in html and "1 × 50 (50 pcs)" in html
    assert "PLA Black" in html and "2 × 1000 g" in html
    assert html.count("Example Supplier") >= 2
    for url in ("https://shop.example/insert-m4", "https://shop.example/pla-black"):
        assert f'href="{url}" target="_blank" rel="noopener noreferrer"' in html
    assert html.count('action="/reorder/mark"') == 2
    assert re.search(r'name="packs"[^>]*value="2"', html)


def test_mark_hides_and_lists_under_way(bb_client):
    pid = add_part(bb_client)
    response = mark_form(bb_client, "part", pid, "1")
    assert response.status_code == 303 and response.headers["location"] == "/reorder"
    html = bb_client.get("/reorder").text
    assert 'action="/reorder/mark"' not in html  # the suggestion is gone
    assert "On its way" in html and "Insert M4" in html and "1 × 50" in html
    assert re.search(r'action="/reorder/marks/\d+/received"', html)


def test_mark_double_submit(bb_client):  # Review Focus 4
    pid = add_part(bb_client)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    data = {"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
            "item_id": str(pid), "packs": "1"}
    first = bb_client.post("/reorder/mark", data=data)
    second = bb_client.post("/reorder/mark", data=data)
    assert first.status_code == 303 and second.status_code == 303
    assert len(marks(bb_client)) == 1
    assert "Already submitted" in bb_client.get("/reorder").text


def test_received_button(bb_client):
    pid = add_part(bb_client)
    mark_form(bb_client, "part", pid, "1")
    html = bb_client.get("/reorder").text
    action = re.search(r'action="(/reorder/marks/\d+/received)"', html).group(1)
    token = form_token_of(bb_client, "/reorder", action)
    response = bb_client.post(action, data={"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token})
    assert response.status_code == 303 and response.headers["location"] == "/reorder"
    assert marks(bb_client)[0].received_at is not None
    assert "On its way" not in bb_client.get("/reorder").text


@pytest.mark.parametrize("bad", ["0", "abc", "9" * 20, "", "1001"])
def test_mark_invalid_packs(bb_client, bad):
    pid = add_part(bb_client)
    response = mark_form(bb_client, "part", pid, bad)
    assert response.status_code == 200 and "Enter a number of order units" in response.text
    assert marks(bb_client) == []
    assert 'action="/reorder/mark"' in response.text  # the form is shown again
    shown = html_escape(bad) if bad else ""
    assert re.search(rf'name="packs"[^>]*value="{re.escape(shown)}"', response.text)  # the input is kept


def test_mark_unknown_item(bb_client):
    add_part(bb_client)  # so that the page has a form to take the token from
    response = mark_form(bb_client, "part", 9999, "1")
    assert response.status_code == 200 and "cannot be ordered" in response.text
    response = mark_form(bb_client, "bogus", "x", "1")
    assert response.status_code == 200 and marks(bb_client) == []


def test_received_unknown_mark(bb_client):
    add_part(bb_client)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post(
        "/reorder/marks/9999/received", data={"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token}
    )
    assert response.status_code == 200 and "does not exist" in response.text


def test_reorder_without_any_filament_stock(bb_client, fake_bambuddy):  # Review Focus 2
    add_part(bb_client)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, safety_value=2000, safety_unit="grams")
    fake_bambuddy.unavailable = True
    response = bb_client.get("/reorder")
    assert response.status_code == 200
    assert "Insert M4" in response.text and "PLA Black" not in response.text
    assert "filament stock is not known" in response.text.lower()


def test_reorder_empty(bb_client):
    assert "Nothing to order" in bb_client.get("/reorder").text


def test_reorder_german(bb_client):
    pid = add_part(bb_client, name="Hex key 2")
    add_part(bb_client)  # stays a suggestion, so the button "mark as ordered" is on the page
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    mark_form(bb_client, "part", pid, "1")
    html = bb_client.get("/reorder").text
    for word in ("Bestellvorschläge", "als bestellt markieren", "Unterwegs", "eingetroffen"):
        assert word in html


def test_nav_reorder_active(bb_client):
    html = bb_client.get("/reorder").text
    active = re.findall(r'class="nav-link active"[^>]*>.*?<span class="nav-label">(.*?)</span>', html, re.S)
    assert set(active) == {"Reorder"}
    assert 'href="/reorder"' in html


def test_stale_filament_suggestion_shows_the_day(bb_client, fake_bambuddy):  # Entscheidung 5
    yesterday = utcnow().date() - timedelta(days=1)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, pack_grams=1000, safety_value=2000, safety_unit="grams")
        assign_sort(s, sort)
        make_snapshot(s, yesterday, 1, sort.id, 100.0)
    fake_bambuddy.unavailable = True
    response = bb_client.get("/reorder")
    assert response.status_code == 200
    assert f"Stock as of {yesterday:%Y-%m-%d}" in response.text
    assert "PLA Black" in response.text and "2 × 1000 g" in response.text


def test_each_suggestion_is_a_tile_with_amount_supplier_link_and_form(bb_client, fake_bambuddy):
    add_part(bb_client)
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    assert "<table" not in html.split("On its way")[0]  # no wide table to scroll sideways
    tiles = re.findall(r'<div class="tile">(.*?)</form>\s*</div>', html, re.S)
    assert len(tiles) == 2
    for tile, amount, url in zip(
        sorted(tiles, key=lambda t: "Insert" not in t),
        ("1 × 50 (50 pcs)", "2 × 1000 g"),
        ("https://shop.example/insert-m4", "https://shop.example/pla-black"),
    ):
        assert amount in tile and "Example Supplier" in tile
        assert f'href="{url}" target="_blank" rel="noopener noreferrer"' in tile
        assert 'action="/reorder/mark"' in tile and 'name="form_token"' in tile
        assert "Usage × lead time" in tile


def test_part_receipt_closes_mark_without_bambuddy(bb_client, fake_bambuddy):  # ruling R7
    pid = add_part(bb_client)
    mark_form(bb_client, "part", pid, "1")
    with sm_of(bb_client).begin() as s:
        s.query(ReorderMark).one().marked_at -= timedelta(seconds=1)
        stock.receive_part(s, pid, 50, "user")
    fake_bambuddy.unavailable = True
    html = bb_client.get("/reorder").text
    assert "On its way" not in html
    assert marks(bb_client)[0].received_at is not None


def test_long_supplier_sits_in_the_detail_line_and_css_wraps_it(bb_client):
    long_name = "Supplier" + "x" * 80
    add_part(bb_client, supplier=long_name)
    html = bb_client.get("/reorder").text
    detail = re.search(r'<p class="reorder-detail">(.*?)</p>', html, re.S).group(1)
    assert long_name in detail
    css = bb_client.get("/static/css/reprintbuddy.css").text
    assert re.search(r"\.reorder-detail \{[^}]*overflow-wrap: anywhere", css)
    assert re.search(r"\.reorder-detail a \{[^}]*var\(--accent\)", css)


def test_under_way_row_has_no_stray_slot_select_class(bb_client):
    part_id = add_part(bb_client)
    mark_form(bb_client, "part", part_id, "1")
    assert "slot-select" not in bb_client.get("/reorder").text


# ---- the look of v0.3.3 (key figures, tiles without a card, table card, symbol button) -----------

from html import unescape  # noqa: E402

from reprintbuddy.web import regions  # noqa: E402
from reprintbuddy.web.i18n import translate  # noqa: E402


def kpi_cards(html):
    return re.findall(r'<span class="kpi-value">(.*?)</span><span class="kpi-label">(.*?)</span>', html)


def summary_of(client):
    return dict((label, value) for value, label in kpi_cards(client.get("/reorder", headers={regions.HEADER: "summary"}).text))


def test_head_keeps_its_sub(bb_client):
    head = re.search(r'<header class="page-head">.*?</header>', bb_client.get("/reorder").text, re.S).group(0)
    assert "#shopping-bag" in head and f'<p class="page-sub">{translate("en", "reorder.sub")}</p>' in head


def test_key_figures_match_the_tiles(bb_client, fake_bambuddy):
    add_part(bb_client)
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    cards = summary_of(bb_client)
    assert cards == {"Suggestions": "2", "Filament": "1", "Parts": "1"}  # nothing on its way: no such card
    assert html.count('<div class="tile">') == 2


def test_key_figures_count_what_is_on_its_way(bb_client):
    pid = add_part(bb_client)
    add_part(bb_client)
    mark_form(bb_client, "part", pid, "1")
    cards = summary_of(bb_client)
    assert cards["On its way"] == "1" and "Suggestions" in cards


def test_summary_region_topics_cover_both_sources(bb_client):
    page = bb_client.get("/reorder").text
    assert 'data-live-region="summary" data-live-topics="filament reorder part article queue journal"' in page


def test_suggestions_are_tiles_without_a_surrounding_card(bb_client, fake_bambuddy):
    add_part(bb_client)
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    assert re.search(r'<div class="collection layout-tiles">\s*<div class="tiles">', html)
    assert "<section" not in html and "section wide" not in html


def test_received_is_a_symbol_button_and_the_row_stays_one_row(bb_client):
    first = add_part(bb_client)
    add_part(bb_client, name="Hex key 2")
    mark_form(bb_client, "part", first, "1")
    html = bb_client.get("/reorder").text
    under_way = regions.extract(html, ["under-way"])["under-way"]
    assert '<div class="card table-wrap table-card">' in under_way
    assert re.search(r'<th class="actions-col"><span class="sr-only">Actions</span></th>', under_way)
    body = under_way.split("<tbody>")[1]
    assert body.count("<tr>") == 1  # one row per mark, no second row for a button
    assert re.search(r'<form class="icon-form" method="post" action="/reorder/marks/\d+/received">', body)
    assert 'class="icon-btn" title="arrived" aria-label="Arrived: Insert M4"' in body and "#check" in body
    assert 'class="btn"' not in under_way
    assert "<h2>" in under_way and "On its way" in under_way


def test_pack_field_has_an_info_button_and_keeps_its_name(bb_client, fake_bambuddy):
    add_part(bb_client)
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    tiles = re.findall(r'<div class="tile">(.*?)</form>\s*</div>', html, re.S)
    assert len(tiles) == 2
    ids = []
    for tile in tiles:
        field_id = re.search(r'<label for="([^"]+)">', tile).group(1)
        ids.append(field_id)
        tag = re.search(r'<input[^>]*\bname="packs"[^>]*>', tile).group(0)
        assert f'id="{field_id}"' in tag and 'value="' in tag
        assert unescape(re.search(r'<div class="info-pop" role="note">(.*?)</div>', tile, re.S).group(1)) \
            == translate("en", "info.reorder_packs")
        assert 'class="info"' not in re.search(r"<label\b.*?</label>", tile, re.S).group(0)
    assert len(set(ids)) == 2  # one id per tile


def test_part_under_min_stock_is_listed_without_any_demand(bb_client):  # p48, Spec 9.3
    with sm_of(bb_client).begin() as s:
        make_part(
            s, name="Washer", stock=18, min_stock=20, pack_size=50, safety_value=0, safety_unit="qty",
        )
    html = bb_client.get("/reorder").text
    assert "Washer" in html
    assert "20 pcs" in html and "Lower limit" in html
    assert "1 × 50 (50 pcs)" in html


# ---- p59: no usage recorded yet ----------------------------------------------------------------

from reprintbuddy.domain import reorder  # noqa: E402
from reprintbuddy.web.i18n import translate  # noqa: E402


def tile_of(html, label):
    return next(t for t in re.findall(r'<div class="tile">(.*?)</form>\s*</div>', html, re.S) if label in t)


def test_a_filament_tile_without_history_says_so(bb_client, fake_bambuddy):  # p59
    add_filament(bb_client, fake_bambuddy)
    tile = tile_of(bb_client.get("/reorder").text, "PLA Black")
    assert translate("en", "filament.no_history") in tile
    assert "0.0 g/day" not in tile


def test_a_filament_tile_with_history_shows_the_use_per_day(bb_client, fake_bambuddy):
    sid = add_filament(bb_client, fake_bambuddy)
    today = utcnow().date()
    with sm_of(bb_client).begin() as s:
        make_snapshot(s, today - timedelta(days=10), 1, sid, 600.0)
        make_snapshot(s, today - timedelta(days=5), 1, sid, 100.0)
    tile = tile_of(bb_client.get("/reorder").text, "PLA Black")
    assert "g/day" in tile and translate("en", "filament.no_history") not in tile


def test_german_tile_without_history(bb_client, fake_bambuddy):
    add_filament(bb_client, fake_bambuddy)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    tile = tile_of(bb_client.get("/reorder").text, "PLA Black")
    assert "noch kein Verlauf" in tile and "0.0 g/Tag" not in tile


# ---- p60: an archived part leaves "on its way" -------------------------------------------------


def test_an_archived_part_leaves_on_its_way_and_comes_back_on_restore(bb_client):  # p60
    from reprintbuddy.domain import catalog

    pid = add_part(bb_client)
    mark_form(bb_client, "part", pid, "1")
    bb_client.get("/reorder")  # shows (and so ends) the flash message that names the part
    with sm_of(bb_client).begin() as s:
        s.query(ArticlePart).delete()  # nothing uses it any more, so it may be archived
        catalog.archive_part(s, pid)
    html = bb_client.get("/reorder").text
    assert "On its way" not in html and "Insert M4" not in html
    assert [m.received_at for m in marks(bb_client)] == [None]  # the mark stays open
    with sm_of(bb_client).begin() as s:
        catalog.restore_part(s, pid)
    html = bb_client.get("/reorder").text
    assert "On its way" in html and "Insert M4: 1 × 50" in html


# ---- p64: the key figure counts what the page shows ---------------------------------------------


def page_tiles(client):
    return len(re.findall(r'<div class="tile">', client.get("/reorder").text))


def counted(client, spools):
    with sm_of(client).begin() as s:
        return reorder.count_suggestions(s, spools, utcnow())


@pytest.mark.parametrize("case", ["live", "record", "nothing", "part_received"])
def test_the_count_is_the_number_of_tiles(bb_client, fake_bambuddy, case):  # p64
    pid = add_part(bb_client)
    sid = add_filament(bb_client, fake_bambuddy)
    spools = None
    if case == "live":
        spools = list(fake_bambuddy._spools.values())
    elif case == "record":
        with sm_of(bb_client).begin() as s:
            make_snapshot(s, utcnow().date() - timedelta(days=1), 1, sid, 100.0)
        fake_bambuddy.unavailable = True
    elif case == "nothing":
        fake_bambuddy.unavailable = True
    else:  # an open part mark whose goods were booked in: the page closes it, the count too
        with sm_of(bb_client).begin() as s:
            # 1 ordered: one receipt of 1 is the whole order (p211); the pack size is the supplier's (Spec 5.3)
            catalog.default_supplier(s, pid).pack_size = 1
        mark_form(bb_client, "part", pid, "1")
        with sm_of(bb_client).begin() as s:
            s.query(ReorderMark).one().marked_at -= timedelta(seconds=1)
            stock.receive_part(s, pid, 1, "user")  # too little: once the mark is closed, it is suggested again
        fake_bambuddy.unavailable = True
    expected = counted(bb_client, spools)
    assert page_tiles(bb_client) == expected
    assert expected == {"live": 2, "record": 2, "nothing": 1, "part_received": 1}[case]


# ---- p182: recorded stock is marked at the numbers, as on the filament page -----------------------


def test_recorded_filament_stock_is_marked_at_the_number(bb_client, fake_bambuddy):
    from reprintbuddy.web.i18n import translate

    yesterday = utcnow().date() - timedelta(days=1)
    pid = add_part(bb_client)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, pack_grams=1000, safety_value=2000, safety_unit="grams")
        assign_sort(s, sort)
        make_snapshot(s, yesterday, 1, sort.id, 100.0)
    fake_bambuddy.unavailable = True
    html = bb_client.get("/reorder").text
    note = translate("en", "filament.as_of", day=f"{yesterday:%Y-%m-%d}")
    tiles = re.findall(r'<div class="tile">(.*?)</form>\s*</div>', html, re.S)
    (film,) = [t for t in tiles if "PLA Black" in t]
    (part,) = [t for t in tiles if "Insert M4" in t]
    stock_label = translate("en", "reorder.col.stock")
    assert re.search(rf'{re.escape(stock_label)} <strong>100 g</strong><span class="col-note">{re.escape(note)}</span>', film)
    assert "col-note" not in part and pid  # the stock of a part is always current
    assert translate("en", "filament.stale", day=f"{yesterday:%Y-%m-%d}") in html  # the notice on top stays


def test_live_filament_stock_is_not_marked(bb_client, fake_bambuddy):
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    assert "PLA Black" in html and "col-note" not in html and "As of" not in html


def test_a_tile_shows_what_is_on_its_way_in_its_group(bb_client, fake_bambuddy):  # p171
    from factories import tick_sorts
    from reprintbuddy.domain import reorder
    from reprintbuddy.models import ArticleFilament
    from reprintbuddy.web.i18n import translate

    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=100.0)
    with sm_of(bb_client).begin() as s:
        a = make_sort(s)
        b = make_sort(s, brand="Brand B")
        for sort in (a, b):
            make_filament_settings(s, sort.id, pack_grams=1000, safety_value=2000, safety_unit="grams")
        art = make_article(s, min_stock=0, stock=0)
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        s.flush()
        tick_sorts(s, art.id, 1, a, b, reordered=a)
        reorder.mark_ordered(s, "filament", b.id, 1)
    html = bb_client.get("/reorder").text
    (tile,) = re.findall(r'<div class="tile">(.*?)</form>\s*</div>', html, re.S)
    assert f'{translate("en", "reorder.under_way")} <strong>1000 g</strong>' in tile
    assert "1 × 1000 g" in tile  # 2000 - (100 + 1000) = 900 missing: one pack instead of two
    add_part(bb_client)
    (part_tile,) = [t for t in re.findall(r'<div class="tile">(.*?)</form>\s*</div>', bb_client.get("/reorder").text, re.S) if "Insert M4" in t]
    assert translate("en", "reorder.under_way") not in part_tile  # nothing on its way: no line


# ---- several suppliers per part (Spec 9.3, 9.4) -----------------------------------------------


def add_second_supplier(client, part_id, **fields):
    from reprintbuddy.models import PartSupplier

    values = dict(name="Example Supplier B", order_url="https://shop.example/b", pack_size=10,
                  min_order_packs=1, lead_time_days=1, position=1)
    values.update(fields)
    with sm_of(client).begin() as s:
        row = PartSupplier(part_id=part_id, **values)
        s.add(row)
        s.flush()
        return row.id


def part_tile(html):
    return re.search(r'<div class="tile">((?:(?!<div class="tile">).)*?Insert M4.*?)</form>\s*</div>', html, re.S).group(1)


def test_a_part_with_two_suppliers_lists_each_with_its_quantity_default_first(bb_client):
    part_id = add_part(bb_client)
    b = add_second_supplier(bb_client, part_id)
    tile = part_tile(bb_client.get("/reorder").text)
    lines = re.findall(r'<li class="reorder-supplier">(.*?)</li>', tile, re.S)
    assert len(lines) == 2
    assert "Example Supplier" in lines[0] and translate("en", "parts.is_default") in lines[0]
    assert "1 × 50 (50 pcs)" in lines[0] and 'href="https://shop.example/insert-m4"' in lines[0]
    assert "Example Supplier B" in lines[1] and "2 × 10 (20 pcs)" in lines[1]  # shortfall 20 + 6 - 10 = 16
    assert 'href="https://shop.example/b" target="_blank" rel="noopener noreferrer"' in lines[1]
    select = re.search(r'<select[^>]*name="supplier_id"[^>]*>(.*?)</select>', tile, re.S).group(1)
    options = re.findall(r'<option value="(\d+)"([^>]*)>', select)
    assert [value for value, _ in options][1] == str(b)
    assert "selected" in options[0][1] and 'data-packs="1"' in options[0][1]


def test_a_part_with_one_supplier_keeps_the_single_line(bb_client):
    add_part(bb_client)
    tile = part_tile(bb_client.get("/reorder").text)
    assert 'name="supplier_id"' not in tile and "reorder-supplier" not in tile
    assert "1 × 50 (50 pcs)" in tile and "Example Supplier" in tile


def test_mark_with_the_second_supplier(bb_client):
    part_id = add_part(bb_client)
    b = add_second_supplier(bb_client, part_id)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(part_id), "packs": "2", "supplier_id": str(b),
    })
    assert response.status_code == 303
    (mark,) = marks(bb_client)
    assert (mark.qty, mark.pack_size, mark.supplier_name) == (2, 10, "Example Supplier B")
    html = bb_client.get("/reorder").text
    under_way = html.split("On its way", 1)[1]
    assert "Insert M4: 2 × 10 pcs" in under_way and "Example Supplier B" in under_way


def test_an_error_keeps_the_chosen_supplier_and_the_typed_packs(bb_client):
    part_id = add_part(bb_client)
    b = add_second_supplier(bb_client, part_id)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(part_id), "packs": "0", "supplier_id": str(b),
    })
    assert response.status_code == 200 and marks(bb_client) == []
    tile = part_tile(response.text)
    assert re.search(rf'<option value="{b}"[^>]*selected', tile)
    assert re.search(r'name="packs"[^>]*value="0"', tile)


def test_a_supplier_of_another_part_is_refused(bb_client):
    part_id = add_part(bb_client)
    with sm_of(bb_client).begin() as s:
        other = make_part(s, name="Screw").id
    foreign = add_second_supplier(bb_client, other, position=1)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(part_id), "packs": "1", "supplier_id": str(foreign),
    })
    assert response.status_code == 200 and marks(bb_client) == []
    assert html_escape(translate("en", "reorder.supplier_not_found")) in response.text


def test_the_supplier_choice_in_german_and_its_script(bb_client):
    from reprintbuddy import __version__

    part_id = add_part(bb_client)
    add_second_supplier(bb_client, part_id)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    html = bb_client.get("/reorder").text
    tile = part_tile(html)
    assert re.search(r'<label for="supplier-part-\d+">Lieferant</label>', tile)
    assert "Standard" in tile
    assert f'<script src="/static/js/reorder.js?v={__version__}" defer></script>' in html
    assert bb_client.get(f"/static/js/reorder.js?v={__version__}").status_code == 200



def test_a_page_shown_before_the_suppliers_changed_marks_nothing(bb_client):  # review R1
    """Saving the suppliers compares by position: removing B moves C into B's row. A page that
    still shows B must not mark C under B's name."""
    from reprintbuddy.domain import catalog

    part_id = add_part(bb_client)
    b = add_second_supplier(bb_client, part_id)
    page = bb_client.get("/reorder").text
    key = re.search(r'name="suppliers_key" value="([^"]+)"', part_tile(page)).group(1)
    with sm_of(bb_client).begin() as s:
        catalog.part_suppliers(s, part_id)[1].name = "Example Supplier C"
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(part_id), "packs": "1", "supplier_id": str(b), "suppliers_key": key,
    })
    assert response.status_code == 200 and marks(bb_client) == []
    assert html_escape(translate("en", "reorder.suppliers_changed")) in response.text


def test_an_unnamed_supplier_is_named_by_its_number(bb_client):  # review R2, Spec 5.3
    part_id = add_part(bb_client, supplier=None)
    tile = part_tile(bb_client.get("/reorder").text)
    assert "Supplier 1" in tile  # the single line names it when there is a link
    b = add_second_supplier(bb_client, part_id, name=None)
    mark_form(bb_client, "part", part_id, "1")
    with sm_of(bb_client).begin() as s:
        s.query(ReorderMark).delete()
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(part_id), "packs": "1", "supplier_id": str(b),
    })
    under_way = bb_client.get("/reorder").text.split("On its way", 1)[1]
    assert "Supplier 2" in under_way


def test_a_number_typed_before_an_error_is_kept_by_the_script(bb_client):  # review R6
    part_id = add_part(bb_client)
    b = add_second_supplier(bb_client, part_id)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(part_id), "packs": "0", "supplier_id": str(b),
    })
    assert re.search(r'name="packs"[^>]*value="0"[^>]*data-typed', part_tile(response.text))
    assert "data-typed" not in part_tile(bb_client.get("/reorder").text)


# ---- p211: a partial delivery shows what has arrived, only the rest is on its way ----------------


def test_a_partial_part_receipt_keeps_the_mark_open_and_shows_what_arrived(bb_client):
    from reprintbuddy.web.formatting import unit_label
    from reprintbuddy.web.i18n import translate

    pid = add_part(bb_client)
    mark_form(bb_client, "part", pid, "1")  # 1 x 50
    with sm_of(bb_client).begin() as s:
        s.query(ReorderMark).one().marked_at -= timedelta(seconds=1)
        stock.receive_part(s, pid, 20, "user")
        unit = s.get(Part, pid).unit
    html = bb_client.get("/reorder").text
    assert translate("en", "reorder.arrived_so_far", amount="20", unit=unit_label(unit, "en")) in html
    with sm_of(bb_client).begin() as s:
        assert s.query(ReorderMark).one().received_at is None
