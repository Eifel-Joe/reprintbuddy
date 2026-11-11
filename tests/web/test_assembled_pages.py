"""Pages with articles made of parts: list, detail, overview, assembly, bookings, orders, sets
(Spec 5.6, 8.3, 10.1-10.3; 0.4.0 p341)."""

import re
from conftest import sm_of
from factories import make_article, make_assembled
from reprintbuddy.domain import assembled
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def world(client):
    """Panels for a box (on stock, no choices, minimum 1); a hose on stock with two variants; a hose
    built on order; a part that is not sold alone."""
    with sm_of(client).begin() as s:
        panel = make_article(s, name="Panel A", stock=5)
        lid = make_article(s, name="Lid", stock=5)
        side = make_article(s, name="Side part L", stock=4, sold_alone=False)
        socket = make_article(s, name="Socket L32", stock=4)
        segment = make_article(s, name="Segment", stock=40)
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 1), (lid.id, 1)], min_stock=1)
        stock_hose = make_assembled(s, name="Bracket", sku="EX-K1", base=[(panel.id, 1)],
                                    fields=[[("-A", [(socket.id, 1)]), ("-B", [(lid.id, 1)])]])
        order_hose = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(segment.id, 12)],
                                    fields=[[("-L32", [(side.id, 1), (socket.id, 1)])]])
        keys = {v.sku: v.variant_key for v in assembled.variants(s, order_hose)}
        variants = {v.sku: v.id for v in assembled.variants(s, stock_hose)}
        return {"panel": panel.id, "lid": lid.id, "side": side.id, "socket": socket.id, "segment": segment.id,
                "box": box.id, "bracket": stock_hose.id, "hose": order_hose.id, "keys": keys, "variants": variants}


# ---- Aufgabe 13: list, detail, overview ----------------------------------------------------------


def test_list_shows_assembled_with_variants(ready_client):
    w = world(ready_client)
    page = ready_client.get("/articles").text
    assert "Storage box" in page and "Bracket" in page and "Hose" in page
    assert "EX-K1-A" in page and "EX-H1-L32" in page
    assert f'href="/articles/{w["variants"]["EX-K1-A"]}"' not in page  # no variant row of its own


def test_part_not_sold_alone_has_grey_badge(ready_client):
    world(ready_client)
    assert en("assembled.part_only") in ready_client.get("/articles").text


def test_search_finds_variant_number(ready_client):
    world(ready_client)
    page = ready_client.get("/articles?q=EX-K1-B").text
    assert "Bracket" in page and "Storage box" not in page


def test_target_field_only_without_choices(ready_client):
    w = world(ready_client)
    page = ready_client.get("/articles").text
    assert f'action="/articles/{w["box"]}/target"' in page
    assert f'action="/articles/{w["bracket"]}/target"' not in page
    assert f'action="/articles/{w["hose"]}/target"' not in page


def test_detail_of_assembled_shows_bom_and_variants(ready_client):
    w = world(ready_client)
    page = ready_client.get(f"/articles/{w['bracket']}")
    assert page.status_code == 200
    assert en("assembled.parts_list") in page.text and "EX-K1-A" in page.text and "EX-K1-B" in page.text
    assert f'action="/assembly/build/{w["variants"]["EX-K1-A"]}"' in page.text
    hose = ready_client.get(f"/articles/{w['hose']}").text
    assert "EX-H1-L32" in hose and "/assembly/build/" not in hose  # built on order: nothing to build here


def test_variant_page_leads_to_its_article(ready_client):
    w = world(ready_client)
    response = ready_client.get(f"/articles/{w['variants']['EX-K1-A']}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{w['bracket']}"


def test_detail_of_print_article_shows_used_in(ready_client):
    w = world(ready_client)
    page = ready_client.get(f"/articles/{w['panel']}").text
    assert en("assembled.used_in") in page
    assert f'href="/articles/{w["box"]}"' in page and f'href="/articles/{w["bracket"]}"' in page


def test_overview_has_one_row_per_holder(ready_client):
    world(ready_client)
    page = ready_client.get("/").text
    assert "EX-B1" in page and "EX-K1-A" in page and "EX-K1-B" in page
    assert "EX-H1-L32" not in page  # built on order: not in the table, like sets


def test_edit_of_print_article_keeps_sold_alone_off(ready_client):
    w = world(ready_client)
    page = ready_client.get(f"/articles/{w['side']}/edit").text
    box = re.search(r'<input name="sold_alone"[^>]*>', page).group(0)
    assert "checked" not in box
