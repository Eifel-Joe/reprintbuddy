"""p533 (Spec 5.2): the article number is a required field in the forms of articles, sets and
articles made of parts, and a save without it says the number is missing."""

import re

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from texts import en
from reprintbuddy.models import Article

FORM = "/articles/new/file/1/plate/1"


def sku_tag(html):
    match = re.search(r'<input[^>]*\bname="sku"[^>]*>', html)
    assert match, "no field sku"
    return match.group(0)


def missing_text():
    return en("form.required", field=en("catalog.field.sku"))


def create_data(client, **over):
    data = {
        "csrf_token": csrf_of(client, FORM), "form_token": form_token_of(client, FORM, FORM),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "model:A1", "sold_alone": "on",
    }
    data.update(over)
    return data


def test_new_article_form_marks_the_number_as_required(bb_client):
    page = bb_client.get(FORM).text
    assert " required" in sku_tag(page)
    assert en("articles.sku_hint") in page


def test_new_article_without_number_is_refused_with_the_message(bb_client):
    response = bb_client.post(FORM, data=create_data(bb_client, sku="  "))
    assert response.status_code == 200 and missing_text() in response.text
    with sm_of(bb_client).begin() as s:
        assert list(s.scalars(select(Article))) == []


def test_new_part_only_may_be_saved_without_number(bb_client):
    data = create_data(bb_client)
    del data["sold_alone"]  # the switch is off
    response = bb_client.post(FORM, data=data)
    assert response.status_code == 303
    with sm_of(bb_client).begin() as s:
        assert [a.sku for a in s.scalars(select(Article))] == [None]


def test_edit_form_follows_the_switch_and_refuses_an_old_article_without_number(ready_client):
    with sm_of(ready_client).begin() as s:
        old = make_article(s, name="Old", sku=None, sold_alone=True).id
        part = make_article(s, name="Part", sku=None, sold_alone=False, library_file_id=2).id
    assert " required" in sku_tag(ready_client.get(f"/articles/{old}/edit").text)
    assert " required" not in sku_tag(ready_client.get(f"/articles/{part}/edit").text)
    path = f"/articles/{old}/edit"
    data = {
        "csrf_token": csrf_of(ready_client, path), "name": "Old", "sku": "", "units_per_print": "1",
        "min_stock": "0", "buffer": "0", "target": "model:A1", "sold_alone": "on",
    }
    response = ready_client.post(path, data=data)
    assert response.status_code == 200 and missing_text() in response.text
    response = ready_client.post(path, data={**data, "sku": "EX-OLD"})
    assert response.status_code == 303


def test_set_and_assembled_forms_mark_the_number_as_required(ready_client):
    assert " required" in sku_tag(ready_client.get("/sets/new").text)
    assert " required" in sku_tag(ready_client.get("/articles/new-assembled").text)
