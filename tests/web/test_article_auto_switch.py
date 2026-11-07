"""The automation switch of each article in the list and the tiles (p44).

One small form with a button in the look of a switch; it sends the wanted state (`enabled=1|0`),
so two open tabs cannot turn each other back. Switching on needs a print target; an archived
article has no switch.
"""

import re
from datetime import datetime, timedelta, timezone

from markupsafe import escape

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.models import Article
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return str(escape(translate("en", key, **params)))


def article(client, name="Template A", **fields) -> int:
    with sm_of(client).begin() as s:
        return make_article(s, name=name, **fields).id


def auto_of(client, article_id) -> bool:
    with sm_of(client)() as s:
        return s.get(Article, article_id).auto_enabled


def switch_post(client, article_id, enabled, *, page="/articles", **extra):
    action = f"/articles/{article_id}/auto"
    data = {
        "csrf_token": csrf_of(client, page),
        "form_token": form_token_of(client, page, action),
        "enabled": enabled,
        "next": page,
        **extra,
    }
    return client.post(action, data=data)


def table_part(page):
    return re.search(r"<table>.*?</table>", page, re.S).group(0)


def tiles_part(page):
    return re.search(r'<div class="tiles">.*', page, re.S).group(0)


def switch_form(part, article_id):
    found = re.search(rf'<form[^>]*action="/articles/{article_id}/auto"[^>]*>.*?</form>', part, re.S)
    assert found, f"no switch for article {article_id}"
    return found.group(0)


# ---- the switch is in the list and in the tiles, with the right state ------------------------


def test_list_and_tiles_have_the_switch_with_state(ready_client):
    on = article(ready_client, "Template A", target_model="A1", auto_enabled=True)
    off = article(ready_client, "Template B", target_model="A1", auto_enabled=False)
    page = ready_client.get("/articles").text
    for part in (table_part(page), tiles_part(page)):
        form_on, form_off = switch_form(part, on), switch_form(part, off)
        assert 'role="switch"' in form_on and 'aria-checked="true"' in form_on
        assert 'name="enabled" value="0"' in form_on  # the form sends the state it wants
        assert 'role="switch"' in form_off and 'aria-checked="false"' in form_off
        assert 'name="enabled" value="1"' in form_off
        assert en("articles.auto_switch", name="Template A") in form_on
        assert en("articles.auto_switch", name="Template B") in form_off
        assert 'name="csrf_token"' in form_on and 'name="form_token"' in form_on
        assert 'name="next" value="/articles"' in form_on


def test_tile_is_not_a_link_around_the_form(ready_client):
    a = article(ready_client, target_model="A1")
    tiles = tiles_part(ready_client.get("/articles").text)
    assert '<a class="tile"' not in tiles  # a form inside a link is not valid
    assert f'href="/articles/{a}"' in tiles


def test_archived_articles_have_no_switch(ready_client):
    a = article(ready_client, target_model="A1", archived_at=datetime.now(timezone.utc))
    page = ready_client.get("/articles?view=archived").text
    assert "Template A" in page and f"/articles/{a}/auto" not in page


def test_german_label(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    a = article(ready_client, target_model="A1")
    page = ready_client.get("/articles").text
    label = str(escape(translate("de", "articles.auto_switch", name="Template A")))
    assert label in switch_form(table_part(page), a)


# ---- switching ----------------------------------------------------------------------------------


def test_switch_on_and_off_writes(ready_client):
    a = article(ready_client, target_model="A1")
    done = switch_post(ready_client, a, "1")
    assert done.status_code == 303 and done.headers["location"] == "/articles"
    assert auto_of(ready_client, a) is True
    page = ready_client.get("/articles").text
    assert 'aria-checked="true"' in switch_form(table_part(page), a)
    assert switch_post(ready_client, a, "0").status_code == 303
    assert auto_of(ready_client, a) is False
    page = ready_client.get("/articles").text
    assert en("articles.auto_off", name="Template A") in page
    assert 'aria-checked="false"' in switch_form(table_part(page), a)


def test_on_message_is_shown(ready_client):
    a = article(ready_client, target_model="A1")
    switch_post(ready_client, a, "1")
    assert en("articles.auto_on", name="Template A") in ready_client.get("/articles").text


def test_wanted_state_is_not_a_toggle(ready_client):
    """Two tabs both asked for "on": the second does not turn it off again."""
    a = article(ready_client, target_model="A1")
    switch_post(ready_client, a, "1")
    switch_post(ready_client, a, "1")
    assert auto_of(ready_client, a) is True


def test_switch_on_with_a_printer_as_target(ready_client):
    a = article(ready_client, target_model=None, printer_id=3)
    assert switch_post(ready_client, a, "1").status_code == 303
    assert auto_of(ready_client, a) is True


def test_switch_keeps_the_view_state(ready_client):
    a = article(ready_client, target_model="A1")
    done = switch_post(ready_client, a, "1", next="/articles?status=enough&q=Tem")
    assert done.headers["location"] == "/articles?status=enough&q=Tem"


def test_next_must_be_local(ready_client):
    a = article(ready_client, target_model="A1")
    done = switch_post(ready_client, a, "1", next="https://elsewhere.example/x")
    assert done.status_code == 303 and done.headers["location"] == "/articles"


def test_without_a_target_switching_on_is_refused(ready_client):
    a = article(ready_client, target_model=None, printer_id=None)
    done = switch_post(ready_client, a, "1")
    assert done.status_code == 303 and done.headers["location"] == "/articles"
    assert auto_of(ready_client, a) is False
    page = ready_client.get("/articles").text
    assert en("articles.auto_needs_target", name="Template A") in page and 'role="alert"' in page


def test_without_a_target_switching_off_always_works(ready_client):
    a = article(ready_client, target_model=None, printer_id=None, auto_enabled=True)
    assert switch_post(ready_client, a, "0").status_code == 303
    assert auto_of(ready_client, a) is False


def test_archived_article_is_refused(ready_client):
    a = article(ready_client, target_model="A1", archived_at=datetime.now(timezone.utc))
    data = {
        "csrf_token": csrf_of(ready_client, f"/articles/{a}"),
        "form_token": form_token_of(ready_client, f"/articles/{a}", f"/articles/{a}/restore"),
        "enabled": "1",
        "next": "/articles",
    }
    assert ready_client.post(f"/articles/{a}/auto", data=data).status_code == 303
    assert auto_of(ready_client, a) is False
    assert en("articles.auto_archived", name="Template A") in ready_client.get("/articles").text


def test_bad_value_and_unknown_article(ready_client):
    a = article(ready_client, target_model="A1")
    assert switch_post(ready_client, a, "maybe").status_code == 303
    assert auto_of(ready_client, a) is False
    assert en("articles.auto_bad_value") in ready_client.get("/articles").text
    missing = ready_client.post(
        "/articles/999/auto",
        data={"csrf_token": csrf_of(ready_client, "/articles"), "form_token": form_token_of(ready_client, "/articles"),
              "enabled": "1"},
    )
    assert missing.status_code == 404


def test_needs_csrf(ready_client):
    a = article(ready_client, target_model="A1")
    token = form_token_of(ready_client, "/articles", f"/articles/{a}/auto")
    assert ready_client.post(f"/articles/{a}/auto", data={"form_token": token, "enabled": "1"}).status_code == 403
    bad = ready_client.post(f"/articles/{a}/auto", data={"csrf_token": "forged", "form_token": token, "enabled": "1"})
    assert bad.status_code == 403
    assert auto_of(ready_client, a) is False


def test_form_token_missing_or_forged_changes_nothing(ready_client):
    a = article(ready_client, target_model="A1")
    action = f"/articles/{a}/auto"
    for token in (None, "forged"):
        data = {"csrf_token": csrf_of(ready_client, "/articles"), "enabled": "1", "next": "/articles"}
        if token:
            data["form_token"] = token
        done = ready_client.post(action, data=data)
        assert done.status_code == 303 and done.headers["location"] == "/articles"
        assert auto_of(ready_client, a) is False
        assert en("form.expired") in ready_client.get("/articles").text


def test_form_token_works_once(ready_client):
    a = article(ready_client, target_model="A1")
    action = f"/articles/{a}/auto"
    data = {
        "csrf_token": csrf_of(ready_client, "/articles"),
        "form_token": form_token_of(ready_client, "/articles", action),
        "enabled": "1",
        "next": "/articles",
    }
    ready_client.post(action, data=data)
    again = ready_client.post(action, data={**data, "enabled": "0"})
    assert again.status_code == 303
    assert auto_of(ready_client, a) is True  # the second submission with the same token does nothing
    assert en("form.duplicate") in ready_client.get("/articles").text


# ---- an update pauses the switch like every other write ------------------------------------------


def test_blocked_while_an_update_runs(ops_env):
    from reprintbuddy.ops.status import UpdateStatus, write_status
    from test_ops_update import data_dir

    a = article(ops_env.client, target_model="A1")
    token = csrf_of(ops_env.client, "/articles")
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=started))
    response = ops_env.client.post(f"/articles/{a}/auto", data={"csrf_token": token, "enabled": "1"})
    assert response.status_code == 503
    assert en("ops.update.in_progress") in response.text
    assert auto_of(ops_env.client, a) is False


# ---- live ------------------------------------------------------------------------------------------


def test_list_region_follows_the_article_topic(ready_client):
    article(ready_client, target_model="A1")
    page = ready_client.get("/articles").text
    assert 'data-live-region="list" data-live-topics="article"' in page
    assert 'role="switch"' in page


def test_switching_reports_the_article(db):
    from reprintbuddy.changes import install_change_feed
    from reprintbuddy.domain import catalog

    with db.begin() as s:
        a = make_article(s, target_model="A1").id
    sent: list[frozenset[str]] = []
    install_change_feed(db, sent.append)
    with db.begin() as s:
        catalog.set_auto_enabled(s, a, True)
    assert sent == [frozenset({"article", f"article:{a}", f"article-settings:{a}"})]
