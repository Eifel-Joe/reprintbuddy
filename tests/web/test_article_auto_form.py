"""The automation tick of the article forms follows the rule of the switch (p52): no print target,
no automation. The message stands at the field, everything typed stays, nothing is saved."""

import re

from markupsafe import escape

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from reprintbuddy.models import Article
from reprintbuddy.web.i18n import translate

FORM = "/articles/new/file/1/plate/1"


def en(key, **params):
    return str(escape(translate("en", key, **params)))


def edit_data(client, a, **over):
    data = {
        "csrf_token": csrf_of(client, f"/articles/{a}/edit"),
        "name": "Renamed", "sku": "", "units_per_print": "2", "min_stock": "3", "buffer": "1",
        "target": "",
    }
    data.update(over)
    return data


def field_error(html):
    found = re.search(r'<p class="field-error" role="alert"[^>]*>(.*?)</p>', html, re.S)
    return found.group(1) if found else None


def checkbox_checked(html, name):
    return bool(re.search(rf'<input name="{name}" type="checkbox" checked>', html))


def test_edit_with_automation_and_no_target_shows_the_error_at_the_field(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, target_model="A1").id
    response = ready_client.post(
        f"/articles/{a}/edit", data=edit_data(ready_client, a, auto_enabled="on")
    )
    assert response.status_code == 200
    assert field_error(response.text) == en("articles.field_auto_needs_target")
    assert 'class="alert"' not in response.text  # not twice: the field says it
    assert 'name="name" value="Renamed"' in response.text  # what was typed stays
    assert 'name="min_stock" inputmode="numeric" value="3"' in response.text
    assert checkbox_checked(response.text, "auto_enabled")
    with sm_of(ready_client)() as s:
        art = s.get(Article, a)
        assert art.name == "Template A" and art.auto_enabled is False and art.target_model == "A1"


def test_edit_with_automation_and_a_target_is_saved(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s).id
    response = ready_client.post(
        f"/articles/{a}/edit",
        data=edit_data(ready_client, a, auto_enabled="on", target="model:A1"),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with sm_of(ready_client)() as s:
        assert s.get(Article, a).auto_enabled is True


def test_edit_that_drops_the_target_of_an_automatic_article_is_refused(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, target_model="A1", auto_enabled=True).id
    response = ready_client.post(
        f"/articles/{a}/edit", data=edit_data(ready_client, a, auto_enabled="on", target="")
    )
    assert field_error(response.text) == en("articles.field_auto_needs_target")
    with sm_of(ready_client)() as s:
        assert s.get(Article, a).target_model == "A1"


def test_edit_without_automation_and_without_target_is_saved(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, target_model="A1").id
    response = ready_client.post(
        f"/articles/{a}/edit", data=edit_data(ready_client, a), follow_redirects=False
    )
    assert response.status_code == 303
    with sm_of(ready_client)() as s:
        assert s.get(Article, a).target_model is None


def test_create_with_automation_and_no_target_shows_the_error_at_the_field(bb_client):
    data = {
        "csrf_token": csrf_of(bb_client, FORM),
        "form_token": form_token_of(bb_client, FORM, FORM),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "", "auto_enabled": "on",
    }
    response = bb_client.post(FORM, data=data)
    assert response.status_code == 200
    assert field_error(response.text) == en("articles.field_auto_needs_target")
    assert checkbox_checked(response.text, "auto_enabled")
    with sm_of(bb_client)() as s:
        assert s.query(Article).count() == 0
