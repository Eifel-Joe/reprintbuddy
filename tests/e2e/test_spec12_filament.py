"""Walk-through of the acceptance step Spec 12 step 9 (filament reorder) over the B6 pages.

Only the in-memory Bambuddy fake is used. The ticker test builds an `Engine` on the app's
sessionmaker by hand; once the engine is wired into the app, it becomes `client.app.state.ticker`.
"""

import re

from conftest import csrf_of, run_tick, sm_of
from factories import make_article, make_filament_settings, make_sort
from reprintbuddy.domain import filament
from reprintbuddy.domain.engine import Engine
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


def test_spec12_step_9(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=300.0)
    article_id = add_template_a(bb_client)

    # Save the mapping: the page preselects the one matching sort, the user takes it over.
    page = bb_client.get(f"/articles/{article_id}/filaments")
    assert page.status_code == 200
    block = re.search(r'<select[^>]*name="slot_1"[^>]*>(.*?)</select>', page.text, re.S).group(1)
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
    html = bb_client.get("/reorder").text
    # (the sort's name may still appear in the "settings saved" notice, so look for the tile itself)
    assert 'action="/reorder/mark"' not in html and "× 1000 g" not in html and "Nothing to order" in html

    # Cushion 2000 g: ceil((2000 + 42.5 - 1100) / 1000) = 1 pack.
    assert settings_form(bb_client, sort_id, 2000).status_code == 303
    html = bb_client.get("/reorder").text
    assert "Example Brand PLA Black" in html and "1 × 1000 g" in html
    assert html.count('action="/reorder/mark"') == 1


# The ticker is built by hand here; it becomes `client.app.state.ticker` once the engine is wired in.
def test_daily_record_through_ticker(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PETG", color_hex="#FFFFFF", color_name="White")
    engine = Engine(sm_of(bb_client), lambda: fake_bambuddy)
    engine.add_poll_hook("filament", filament.poll_hook)

    run_tick(engine, poll=True)

    assert "filament" not in engine.status.hook_errors
    with sm_of(bb_client)() as s:
        assert s.query(FilamentSort).count() == 2
        assert s.query(FilamentSnapshot).filter_by(day=utcnow().date()).count() == 2


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
