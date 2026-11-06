"""Walk-through of the acceptance step Spec 12 step 9 (filament reorder) over the B6 pages.

Only the in-memory Bambuddy fake is used. The ticker test runs the app's own `app.state.ticker`.
"""

import re

from conftest import csrf_of, run_tick, sm_of
from factories import make_article, make_filament_settings, make_sort
from reprintbuddy.models import ArticleFilament, FilamentSettings, FilamentSnapshot, FilamentSort, utcnow


def add_template_a(client):
    """Template A: slot 1 PLA / #000000, 42.5 g; stock 0, minimum stock 1, buffer 0, automation off."""
    with sm_of(client).begin() as s:
        art = make_article(s, name="Template A", stock=0, min_stock=1, buffer=0, auto_enabled=False)
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        return art.id


def settings_form(client, sort_id, safety):
    data = {
        "csrf_token": csrf_of(client, f"/filament/{sort_id}"), "lead_time_days": "0", "safety_value": str(safety),
        "safety_unit": "grams", "pack_grams": "1000", "min_order_packs": "1",
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


def test_spec12_step_9(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=300.0)
    article_id = add_template_a(bb_client)

    # Save the mapping: the page preselects the one matching sort, the user takes it over.
    page = bb_client.get(f"/articles/{article_id}/filaments")
    assert page.status_code == 200
    found = re.search(r'<select[^>]*name="slot_1"[^>]*>(.*?)</select>', page.text, re.S)
    assert found, "no slot_1 select on the mapping page"
    block = found.group(1)
    selected = re.findall(r'<option value="(\d+)" selected>([^<]*)</option>', block)
    assert len(selected) == 1 and selected[0][1] == "Example Brand PLA Black"
    sort_id = int(selected[0][0])
    response = bb_client.post(
        f"/articles/{article_id}/filaments",
        data={"csrf_token": csrf_of(bb_client, f"/articles/{article_id}/filaments"), "slot_1": str(sort_id)},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{article_id}"
    with sm_of(bb_client).begin() as s:
        assert s.query(ArticleFilament).filter_by(article_id=article_id, slot=1).one().spool_filament_id == sort_id

    # Pack 1000 g, minimum order 1, lead time 0, cushion 0 g: 1100 - 42.5 >= 0, no filament suggestion.
    assert settings_form(bb_client, sort_id, 0).status_code == 303
    assert no_tile(bb_client)

    # Cushion 2000 g: ceil((2000 + 42.5 - 1100) / 1000) = 1 pack; the tile shows stock, demand, cushion.
    assert settings_form(bb_client, sort_id, 2000).status_code == 303
    tile = only_tile(bb_client, sort_id)
    assert stat(tile, "Stock") == "1100 g"
    assert stat(tile, "Demand") == "42 g"  # 42.5 g shown with :.0f; the next steps pin the exact value
    assert stat(tile, "Safety cushion") == "2000 g"
    assert "Suggestion: 1 × 1000 g" in tile

    # The demand is exactly 42.5 g: 1100 - 42.5 = 1057.5 >= 1057, but < 1058.
    assert settings_form(bb_client, sort_id, 1057).status_code == 303
    assert no_tile(bb_client)
    assert settings_form(bb_client, sort_id, 1058).status_code == 303
    assert "Suggestion: 1 × 1000 g" in only_tile(bb_client, sort_id)


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
