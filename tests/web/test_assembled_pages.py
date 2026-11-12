"""Pages with articles made of parts: list, detail, overview, assembly, bookings, orders, sets
(Spec 5.6, 8.3, 10.1-10.3; 0.4.0 p341)."""

import re
from datetime import date

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_assembled
from reprintbuddy.domain import assembled, orders
from reprintbuddy.domain.orders import AssembledItem
from reprintbuddy.models import Article, Order
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


def test_overview_counts_an_article_of_parts_once_like_the_articles_page(ready_client):
    """Since 0.4.2 (Spec 10.2, p420) the stock tile counts per article like the articles page: an article
    on stock with two overbooked variants is one overbooked article, and the filter it leads to lists it."""
    w = world(ready_client)
    with sm_of(ready_client).begin() as s:
        for variant_id in w["variants"].values():
            s.get(Article, variant_id).reserved = 1  # overbooked: available below 0
    page = ready_client.get("/").text
    assert "EX-B1" not in page and "EX-K1-A" not in page  # no numbers per article
    link = re.search(r'<a class="ov-sub[^"]*" href="/articles\?status=overbooked">(\d+) ', page)
    assert link, "no overbooked part on the stock tile"
    listed = set(re.findall(r'href="/articles/(\d+)"', ready_client.get("/articles?status=overbooked").text.split("<!--live:list-->")[1]))
    assert int(link.group(1)) == len(listed) == 1
    assert listed == {str(w["bracket"])}  # the article on stock with the two variants, counted once


def test_edit_of_print_article_keeps_sold_alone_off(ready_client):
    w = world(ready_client)
    page = ready_client.get(f"/articles/{w['side']}/edit").text
    box = re.search(r'<input name="sold_alone"[^>]*>', page).group(0)
    assert "checked" not in box


# ---- Aufgabe 14: assembly list and QR code ----------------------------------------------------


def test_assembly_lists_box_below_target_with_buildable(ready_client):
    w = world(ready_client)
    page = ready_client.get("/assembly").text
    assert en("assembly.from_parts") in page
    assert f'action="/assembly/build/{w["box"]}"' in page
    assert en("assembly.buildable", n=5) in page


def test_build_from_the_list(ready_client):
    w = world(ready_client)
    data = {"csrf_token": csrf_of(ready_client, "/assembly"),
            "form_token": form_token_of(ready_client, "/assembly", f"/assembly/build/{w['box']}"), "qty": "1"}
    response = ready_client.post(f"/assembly/build/{w['box']}", data=data, follow_redirects=False)
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert (s.get(Article, w["box"]).stock, s.get(Article, w["panel"]).stock) == (1, 4)


def test_build_refused_shows_the_short_part(ready_client):
    w = world(ready_client)
    data = {"csrf_token": csrf_of(ready_client, "/assembly"),
            "form_token": form_token_of(ready_client, "/assembly", f"/assembly/build/{w['box']}"), "qty": "9"}
    page = ready_client.post(f"/assembly/build/{w['box']}", data=data)
    assert page.status_code == 200
    assert en("assembled.part_short", name="Panel A", available=5, needed=9) in page.text


def test_orders_section_is_read_only(ready_client):
    w = world(ready_client)
    with sm_of(ready_client).begin() as s:
        orders.create_order(s, reference="R-77", order_date=date(2026, 11, 11),
                            items=[AssembledItem(w["hose"], 1, w["keys"]["EX-H1-L32"])], actor="user")
    page = ready_client.get("/assembly").text
    assert en("assembly.for_orders") in page and "R-77" in page and "EX-H1-L32" in page
    assert f'/assembly/build/{w["hose"]}' not in page


def test_qr_of_assembled_goes_to_assembly_or_page(ready_client):
    w = world(ready_client)
    response = ready_client.get(f"/a/{w['box']}", follow_redirects=False)
    assert response.headers["location"].startswith("/assembly") and str(w["box"]) in response.headers["location"]
    response = ready_client.get(f"/a/{w['hose']}", follow_redirects=False)
    assert response.headers["location"] == f"/articles/{w['hose']}"


# ---- Aufgabe 15: bookings, order form, sets -------------------------------------------------------


def _select(page: str, form_action: str) -> str:
    form = re.search(rf'<form[^>]*action="{form_action}".*?</form>', page, re.S).group(0)
    return re.search(r'<select name="article_id"[^>]*>(.*?)</select>', form, re.S).group(1)


def test_bookings_offer_holders_and_print_articles(ready_client):
    w = world(ready_client)
    page = ready_client.get("/bookings").text
    count = _select(page, "/bookings/inventory")
    assert f'value="{w["box"]}"' in count and "Bracket · EX-K1-A" in count
    assert f'value="{w["bracket"]}"' not in count and f'value="{w["hose"]}"' not in count
    sale = _select(page, "/bookings/sale")
    assert f'value="assembled:{w["hose"]}:{w["keys"]["EX-H1-L32"]}"' in sale


def test_stock_in_offers_only_print_articles(ready_client):
    w = world(ready_client)
    stock_in = _select(ready_client.get("/bookings").text, "/bookings/stock-in")
    assert f'value="{w["panel"]}"' in stock_in and f'value="{w["box"]}"' not in stock_in


def test_sell_a_hose_variant_from_bookings(ready_client):
    w = world(ready_client)
    data = {"csrf_token": csrf_of(ready_client, "/bookings"),
            "form_token": form_token_of(ready_client, "/bookings", "/bookings/sale"),
            "article_id": f"assembled:{w['hose']}:{w['keys']['EX-H1-L32']}", "qty": "1"}
    response = ready_client.post("/bookings/sale", data=data, follow_redirects=False)
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert (s.get(Article, w["segment"]).stock, s.get(Article, w["side"]).stock) == (28, 3)


def test_order_form_offers_hose_variants_and_hides_parts(ready_client):
    w = world(ready_client)
    page = ready_client.get("/orders/new").text
    group = re.search(r'<select name="item_0">(.*?)</select>', page, re.S).group(1)
    assert f'value="assembled:{w["hose"]}:{w["keys"]["EX-H1-L32"]}"' in group
    assert "Hose · EX-H1-L32 (Side part L, Socket L32)" in group
    assert f'value="article:{w["side"]}"' not in group  # a part only
    assert f'value="article:{w["bracket"]}"' not in group  # with choices: its variants instead
    assert f'value="article:{w["variants"]["EX-K1-A"]}"' in group


def test_order_detail_shows_assembled_group(ready_client):
    w = world(ready_client)
    data = {"csrf_token": csrf_of(ready_client, "/orders/new"),
            "form_token": form_token_of(ready_client, "/orders/new", "/orders/new"),
            "reference": "R-5", "order_date": "2026-11-11", "rows": "1",
            "item_0": f"assembled:{w['hose']}:{w['keys']['EX-H1-L32']}", "qty_0": "2"}
    response = ready_client.post("/orders/new", data=data, follow_redirects=False)
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        order_id = s.scalars(select(Order.id)).one()
    page = ready_client.get(f"/orders/{order_id}").text
    assert "Hose" in page and "EX-H1-L32" in page and "Segment" in page


def test_set_form_hides_parts_not_sold_alone(ready_client):
    w = world(ready_client)
    page = ready_client.get("/sets/new").text
    base = re.search(r'<select name="base_0_article"[^>]*>(.*?)</select>', page, re.S).group(1)
    assert f'value="{w["panel"]}"' in base and f'value="{w["box"]}"' in base
    for hidden in ("side", "bracket", "hose"):
        assert f'value="{w[hidden]}"' not in base, hidden
