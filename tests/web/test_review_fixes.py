"""Findings of the whole-branch review: each has a test that failed before the fix."""

import re

import pytest

from conftest import csrf_of, sm_of
from factories import make_article
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import BaseInput, SetInput
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import Article, utcnow
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def test_set_edit_keeps_base_parts_with_archived_articles(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [BaseInput(1, a), BaseInput(2, b)], [])).id
        s.get(Article, b).archived_at = utcnow()  # archiving it is refused now (0.4.3); an earlier version left it so
    page = ready_client.get(f"/sets/{set_id}/edit").text
    assert re.search(rf'<option value="{b}" selected>', page)  # still offered, and selected
    assert en("catalog.archived_badge") in page
    response = ready_client.post(
        f"/sets/{set_id}/edit",
        data={"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Renamed", "sku": "",
              "base_rows": "3", "base_0_qty": "1", "base_0_article": str(a), "base_1_qty": "2", "base_1_article": str(b)},
    )
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        row = catalog.list_sets(s)[0]
        assert row.name == "Renamed" and len(row.base) == 2
        assert row.orderable is True  # Spec 5.4 Altfall (0.4.3): an archived part no longer takes it out


def test_update_set_may_keep_but_not_add_archived_articles(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        c = make_article(s, name="Template C").id
        set_id = catalog.create_set(s, SetInput("S", None, [BaseInput(1, a), BaseInput(1, b)], [])).id
        s.get(Article, b).archived_at = utcnow()  # archiving it is refused now (0.4.3); an earlier version left it so
        catalog.archive_article(s, c)
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("S2", None, [BaseInput(1, a), BaseInput(1, b)], []))  # b was already there
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_set(s, set_id, SetInput("S3", None, [BaseInput(1, a), BaseInput(1, c)], []))
    assert info.value.key == "sets.article_archived"


def test_set_with_many_base_parts_survives_an_edit(ready_client):
    with sm_of(ready_client).begin() as s:
        ids = [make_article(s, name=f"Template {i:02d}").id for i in range(49)]
        set_id = catalog.create_set(s, SetInput("Big Set", None, [BaseInput(1, a) for a in ids], [])).id
    page = ready_client.get(f"/sets/{set_id}/edit").text
    rows = int(re.search(r'name="base_rows" value="(\d+)"', page).group(1))
    assert rows >= 49
    data = {"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Big Set", "sku": "",
            "base_rows": str(rows), "c1_rows": "3"}
    for i, a in enumerate(ids):
        data[f"base_{i}_qty"] = "1"
        data[f"base_{i}_article"] = str(a)
    assert ready_client.post(f"/sets/{set_id}/edit", data=data).status_code == 303
    with sm_of(ready_client).begin() as s:
        assert len(catalog.list_sets(s)[0].base) == 49


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
    data = {"csrf_token": csrf_of(ready_client, "/sets/new"), "form_token": "x", "name": "S", "base_rows": "²",
            "c1_rows": "²", "base_0_qty": "1", "base_0_article": "²", "c1_0_article": "²"}
    assert ready_client.post("/sets/new", data=data).status_code in (200, 400)
