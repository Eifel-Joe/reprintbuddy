"""Findings of the whole-branch review: each has a test that failed before the fix."""

import re

import pytest

from conftest import csrf_of, sm_of
from factories import make_article
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import PositionInput, SetInput
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def test_set_edit_keeps_positions_with_archived_articles(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        set_id = catalog.create_set(
            s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(2, [b])])
        ).id
        catalog.archive_article(s, b)
    page = ready_client.get(f"/sets/{set_id}/edit").text
    assert re.search(rf'<option value="{b}" selected>', page)  # still offered, and selected
    assert en("catalog.archived_badge") in page
    response = ready_client.post(
        f"/sets/{set_id}/edit",
        data={"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Renamed", "sku": "",
              "rows": "5", "pos_0_qty": "1", "pos_0_articles": [str(a)], "pos_1_qty": "2", "pos_1_articles": [str(b)]},
    )
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        row = catalog.list_sets(s)[0]
        assert row.name == "Renamed" and len(row.positions) == 2
        assert row.orderable is False  # position 2 has no active article, as before


def test_update_set_may_keep_but_not_add_archived_articles(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        c = make_article(s, name="Template C").id
        set_id = catalog.create_set(s, SetInput("S", None, [PositionInput(1, [a, b])])).id
        catalog.archive_article(s, b)
        catalog.archive_article(s, c)
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("S2", None, [PositionInput(1, [a, b])]))  # b was already there
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_set(s, set_id, SetInput("S3", None, [PositionInput(1, [a, c])]))
    assert info.value.key == "sets.article_archived"


def test_set_with_many_positions_survives_an_edit(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        set_id = catalog.create_set(s, SetInput("Big Set", None, [PositionInput(1, [a]) for _ in range(49)])).id
    page = ready_client.get(f"/sets/{set_id}/edit").text
    rows = int(re.search(r'name="rows" value="(\d+)"', page).group(1))
    assert rows >= 49
    data = {"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Big Set", "sku": "", "rows": str(rows)}
    for i in range(49):
        data[f"pos_{i}_qty"] = "1"
        data[f"pos_{i}_articles"] = [str(a)]
    assert ready_client.post(f"/sets/{set_id}/edit", data=data).status_code == 303
    with sm_of(ready_client).begin() as s:
        assert len(catalog.list_sets(s)[0].positions) == 49


HUGE = "9" * 25


@pytest.mark.parametrize("path", [
    f"/orders/{HUGE}", f"/articles/{HUGE}", f"/articles/{HUGE}/edit", f"/articles/{HUGE}/thumbnail",
    f"/articles/{HUGE}/relink", f"/parts/{HUGE}/edit", f"/sets/{HUGE}/edit",
])
def test_huge_ids_in_the_path_are_404(ready_client, path):
    assert ready_client.get(path).status_code == 404


@pytest.mark.parametrize("path", [
    f"/orders/{HUGE}/ship", f"/orders/{HUGE}/cancel", f"/articles/{HUGE}/archive",
    f"/parts/{HUGE}/restore", f"/sets/{HUGE}/archive",
])
def test_huge_ids_in_posts_are_404(ready_client, path):
    data = {"csrf_token": csrf_of(ready_client, "/orders"), "form_token": "x"}
    assert ready_client.post(path, data=data).status_code == 404


def test_unicode_digits_do_not_cause_a_500(ready_client):
    for path in ("/journal?article=²", "/journal?page=٣", "/articles/new?folder=²"):
        assert ready_client.get(path).status_code in (200, 404), path  # no folder without a connection
    with sm_of(ready_client).begin() as s:
        a = make_article(s).id
    for over in ({"rows": "²"}, {"item_0": "set:²"}, {"item_0": f"article:{a}", "qty_0": "²"}):
        data = {
            "csrf_token": csrf_of(ready_client, "/orders/new"), "form_token": "x",
            "reference": "Order 1001", "order_date": "2026-01-15", "note": "", "rows": "5",
            "item_0": f"article:{a}", "qty_0": "1",
        }
        data.update(over)
        assert ready_client.post("/orders/new", data=data).status_code in (200, 400), over
    data = {"csrf_token": csrf_of(ready_client, "/sets/new"), "form_token": "x", "name": "S", "rows": "²",
            "pos_0_qty": "1", "pos_0_articles": ["²"]}
    assert ready_client.post("/sets/new", data=data).status_code in (200, 400)
