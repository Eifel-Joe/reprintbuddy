"""Walk-through of the acceptance step Spec 12 step 9 (filament reorder of the reordered spool sort of a slot).

Only the in-memory Bambuddy fake is used. The ticker test runs the app's own `app.state.ticker`.
"""

import re

from conftest import csrf_of, run_tick, sm_of
from factories import make_article, make_filament_settings, make_sort
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort, FilamentSettings, FilamentSnapshot, FilamentSort, utcnow


def add_template_a(client):
    """Template A: slot 1 PLA / #000000, 42.5 g; stock 0, minimum stock 1, buffer 0, automation off."""
    with sm_of(client).begin() as s:
        art = make_article(s, name="Template A", stock=0, min_stock=1, buffer=0, auto_enabled=False)
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        return art.id


def settings_form(client, sort_id, safety):
    data = {
        "csrf_token": csrf_of(client, f"/filament/{sort_id}"), "lead_time_days": "0", "safety_value": str(safety),
        "safety_unit": "grams", "pack_size": "1000", "min_order_packs": "1",
        "supplier": "Example Supplier", "order_url": "https://shop.example/pla-black",
    }
    return client.post(f"/filament/{sort_id}", data=data, follow_redirects=False)


def tiles(client):
    html = client.get("/reorder").text
    return re.findall(r'<div class="tile">(.*?)</form>\s*</div>', html, re.S)


def no_tile(client):
    return tiles(client) == []


def only_tile(client, sort_id):
    found = tiles(client)
    assert len(found) == 1
    tile = found[0]
    assert 'name="item_type" value="filament"' in tile
    assert f'name="item_id" value="{sort_id}"' in tile
    return tile


def stat(tile, caption):
    found = re.search(rf"<span>{caption} <strong>([^<]*)</strong>", tile)
    assert found, caption
    return found.group(1)


def tick_form(client, article_id, *ticks, reorder=None):
    data = {"csrf_token": csrf_of(client, f"/articles/{article_id}/filaments"), "slot": "1",
            "tick_1": [str(t) for t in ticks]}
    if reorder is not None:
        data["reorder_1"] = str(reorder)
    return client.post(f"/articles/{article_id}/filaments", data=data, follow_redirects=False)


def test_spec12_step_9(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", brand="Brand A", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake_bambuddy.add_spool("PLA", brand="Brand A", color_hex="#000000", color_name="Black", remaining_g=300.0)
    fake_bambuddy.add_spool("PLA", brand="Brand B", color_hex="#000000", color_name="Black", remaining_g=500.0)
    article_id = add_template_a(bb_client)

    # The slot offers both spool sorts of the colour, with brand; nothing is ticked yet (Spec 5.2).
    page = bb_client.get(f"/articles/{article_id}/filaments")
    assert page.status_code == 200
    offered = re.findall(r'<input type="checkbox"[^>]*name="tick_1" value="(\d+)"( checked)?', page.text)
    assert len(offered) == 2 and not any(checked for _, checked in offered)
    with sm_of(bb_client).begin() as s:
        brand_a = s.query(FilamentSort).filter_by(brand="Brand A").one().id
        brand_b = s.query(FilamentSort).filter_by(brand="Brand B").one().id

    # Only Brand A is ticked: it is reordered by itself, and the other brand does not count.
    response = tick_form(bb_client, article_id, brand_a)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{article_id}"
    with sm_of(bb_client).begin() as s:
        rows = s.query(ArticleFilamentSort).filter_by(article_id=article_id, slot=1).all()
        assert [(r.sort_id, r.reordered) for r in rows] == [(brand_a, True)]

    # Pack 1000 g, minimum order 1, lead time 0, cushion 0 g: 1100 - 42.5 >= 0, no filament suggestion.
    assert settings_form(bb_client, brand_a, 0).status_code == 303
    assert no_tile(bb_client)

    # Cushion 2000 g: ceil((2000 + 42.5 - 1100) / 1000) = 1 pack; the tile shows stock, demand, cushion.
    assert settings_form(bb_client, brand_a, 2000).status_code == 303
    tile = only_tile(bb_client, brand_a)
    assert stat(tile, "Stock") == "1100 g"  # Brand B's 500 g are not ticked, so they do not count
    assert stat(tile, "Demand") == "42 g"  # 42.5 g shown with :.0f; the next steps pin the exact value
    assert stat(tile, "Lower limit") == "2000 g"
    assert "Suggestion: 1 × 1000 g" in tile

    # The demand is exactly 42.5 g: 1100 - 42.5 = 1057.5 >= 1057, but < 1058.
    assert settings_form(bb_client, brand_a, 1057).status_code == 303
    assert no_tile(bb_client)
    assert settings_form(bb_client, brand_a, 1058).status_code == 303
    assert "Suggestion: 1 × 1000 g" in only_tile(bb_client, brand_a)

    # Brand B ticked as well, Brand A still reordered: the stock of both counts, no suggestion for B.
    assert tick_form(bb_client, article_id, brand_a, brand_b, reorder=brand_a).status_code == 303
    assert settings_form(bb_client, brand_a, 1558).status_code == 303
    assert stat(only_tile(bb_client, brand_a), "Stock") == "1600 g"
    assert settings_form(bb_client, brand_a, 1557).status_code == 303
    assert no_tile(bb_client)


def test_daily_record_through_ticker(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PETG", color_hex="#FFFFFF", color_name="White")
    engine = bb_client.app.state.ticker
    today = utcnow().date()

    run_tick(engine, poll=True)

    assert "filament" not in engine.status.hook_errors
    with sm_of(bb_client)() as s:
        assert s.query(FilamentSort).count() == 2
        assert s.query(FilamentSnapshot).filter_by(day=today).count() == 2


def test_all_b6_get_pages_without_bambuddy(ready_client):
    article_id = add_template_a(ready_client)
    for path in ("/filament", "/reorder", f"/articles/{article_id}/filaments"):
        assert ready_client.get(path).status_code == 200, path
    with sm_of(ready_client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, pack_grams=1000)
        sort_id = sort.id
    assert ready_client.get(f"/filament/{sort_id}").status_code == 200
    with sm_of(ready_client).begin() as s:
        assert s.get(FilamentSettings, sort_id) is not None
