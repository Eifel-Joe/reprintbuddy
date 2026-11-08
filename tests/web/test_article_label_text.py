"""The article field "label text" (Spec 5.2): free text for the label, e.g. the shelf compartment.
Optional, empty by default, at most 60 characters, shown on the article's page."""

import re

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article

from reprintbuddy.models import Article
from reprintbuddy.web.i18n import translate

FORM = "/articles/new/file/1/plate/1"


def en(key, **params):
    return translate("en", key, **params)


def edit_data(client, a, **over):
    data = {
        "csrf_token": csrf_of(client, f"/articles/{a}/edit"),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0", "target": "",
    }
    data.update(over)
    return data


def stored(client, a):
    with sm_of(client)() as s:
        return s.get(Article, a).label_text


@pytest.fixture
def article(ready_client):
    with sm_of(ready_client).begin() as s:
        return make_article(s, target_model="A1").id


def test_the_edit_form_has_the_field_empty_by_default(ready_client, article):
    page = ready_client.get(f"/articles/{article}/edit").text
    field = re.search(r'<input[^>]*name="label_text"[^>]*>', page)
    assert field and 'maxlength="60"' in field.group(0)
    assert 'value=""' in field.group(0)
    assert en("articles.field.label_text") in page


def test_the_text_is_saved_trimmed_and_shown_on_the_page_and_in_the_form(ready_client, article):
    done = ready_client.post(
        f"/articles/{article}/edit", data=edit_data(ready_client, article, label_text="  Shelf B3  ")
    )
    assert done.status_code == 303
    assert stored(ready_client, article) == "Shelf B3"
    page = ready_client.get(f"/articles/{article}").text
    assert en("articles.field.label_text") in page and "Shelf B3" in page
    assert 'value="Shelf B3"' in ready_client.get(f"/articles/{article}/edit").text


def test_an_empty_text_removes_it(ready_client, article):
    with sm_of(ready_client).begin() as s:
        s.get(Article, article).label_text = "Shelf B3"
    ready_client.post(f"/articles/{article}/edit", data=edit_data(ready_client, article, label_text="   "))
    assert stored(ready_client, article) is None


def test_sixty_characters_are_fine_sixty_one_are_not(ready_client, article):
    ok = ready_client.post(f"/articles/{article}/edit", data=edit_data(ready_client, article, label_text="x" * 60))
    assert ok.status_code == 303 and stored(ready_client, article) == "x" * 60
    too_long = ready_client.post(
        f"/articles/{article}/edit", data=edit_data(ready_client, article, label_text="y" * 61)
    )
    assert too_long.status_code == 200
    assert en("form.too_long", field=en("articles.field.label_text"), max=60) in too_long.text.replace("&#34;", '"')
    assert stored(ready_client, article) == "x" * 60  # nothing was saved
    assert 'value="' + "y" * 61 in too_long.text  # what was typed stays


def test_a_new_article_takes_the_text_too(bb_client):
    data = {
        "csrf_token": csrf_of(bb_client, FORM),
        "form_token": form_token_of(bb_client, FORM, FORM),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "model:A1", "label_text": "Drawer 2",
    }
    assert bb_client.post(FORM, data=data).status_code == 303
    with sm_of(bb_client)() as s:
        assert s.scalars(__import__("sqlalchemy").select(Article)).one().label_text == "Drawer 2"


def test_the_change_is_in_the_journal_of_the_settings(ready_client, article):
    from reprintbuddy.changes import SETTINGS_COLUMNS

    assert "label_text" in SETTINGS_COLUMNS[Article]
