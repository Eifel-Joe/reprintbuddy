"""The filament page marks a spool sort with a reorder suggestion as "low" (Spec 9.4, 0.4.1 p381).

The overview counts these sorts on its card "Filament" and leads here; the badge shows which ones.
The suggestions are the reorder page's (same function); a sort marked as ordered is on its way, not low.
"""

import re

from conftest import csrf_of, form_token_of, sm_of
from factories import assign_sort, make_filament_settings, make_sort
from reprintbuddy.web.i18n import translate

BADGE = f'<span class="badge tone-warn">{translate("en", "filament.short")}</span>'


def row(html, label):
    found = [r for r in re.findall(r"<tr>(.*?)</tr>", html, re.S) if label in r]
    assert len(found) == 1, label
    return found[0]


def short_sort(client, fake, *, grams=300.0):
    """A reordered PLA Black with 300 g and a safety margin of 1000 g: a suggestion."""
    fake.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=grams)
    with sm_of(client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, safety_value=1000, safety_unit="grams", pack_grams=1000, min_order_packs=1)
        assign_sort(s, sort)
        return sort.id


def test_a_sort_with_a_suggestion_is_low(bb_client, fake_bambuddy):
    short_sort(bb_client, fake_bambuddy)
    assert BADGE in row(bb_client.get("/filament").text, "PLA Black")


def test_enough_is_not_low(bb_client, fake_bambuddy):
    short_sort(bb_client, fake_bambuddy, grams=5000.0)
    assert BADGE not in row(bb_client.get("/filament").text, "PLA Black")


def test_ordered_is_on_its_way_not_low(bb_client, fake_bambuddy):
    sort_id = short_sort(bb_client, fake_bambuddy)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    marked = bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "filament",
        "item_id": str(sort_id), "packs": "1",
    })
    assert marked.status_code == 303
    assert BADGE not in row(bb_client.get("/filament").text, "PLA Black")


def test_the_list_follows_the_reorder_marks(bb_client):
    assert 'data-live-region="list" data-live-topics="filament article queue reorder"' in bb_client.get("/filament").text
