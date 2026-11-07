"""The QR code on the article's page and the setting "Address for QR codes" (Spec 8.4)."""

import html
import re

import pytest
from conftest import csrf_of, sm_of
from factories import make_article

from reprintbuddy import settings as st
from reprintbuddy.web.i18n import translate

BASE = "https://shelf.example.com"


def en(key, **params):
    return translate("en", key, **params)


def de(key, **params):
    return translate("de", key, **params)


@pytest.fixture
def article_id(ready_client):
    with sm_of(ready_client).begin() as s:
        return make_article(s, name="Template A").id


def stored(client):
    with sm_of(client)() as s:
        return st.get_setting(s, "qr_base_url")


def save(client, value, **extra):
    data = {"csrf_token": csrf_of(client, "/settings"), "qr_base_url": value}
    data.update(extra)
    return client.post("/settings/qr", data=data)


# ---- the article's page ----


def test_page_shows_the_code_the_address_and_both_downloads(ready_client, article_id):
    page = ready_client.get(f"/articles/{article_id}").text
    assert en("qr.title") in page
    assert f'src="/articles/{article_id}/qr.svg"' in page
    assert f"http://testserver/a/{article_id}" in page
    assert f'href="/articles/{article_id}/qr.png?download=1"' in page
    assert f'href="/articles/{article_id}/qr.svg?download=1"' in page


def test_page_in_german(ready_client, article_id):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get(f"/articles/{article_id}").text
    assert de("qr.title") in page and de("qr.download_png") in page
    assert en("qr.download_png") not in page


def test_page_uses_the_address_of_the_setting(ready_client, article_id):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "qr_base_url", BASE, commit=False)
    page = ready_client.get(f"/articles/{article_id}").text
    assert f"{BASE}/a/{article_id}" in page
    assert "testserver/a/" not in page


def test_archived_article_keeps_its_code_on_the_page(ready_client, article_id):
    from datetime import UTC, datetime

    from reprintbuddy.models import Article

    with sm_of(ready_client).begin() as s:
        s.get(Article, article_id).archived_at = datetime.now(UTC)
    assert f"/articles/{article_id}/qr.svg" in ready_client.get(f"/articles/{article_id}").text


def test_no_warning_for_an_address_a_phone_can_reach(ready_client, article_id):
    assert en("qr.local_warning") not in ready_client.get(f"/articles/{article_id}").text


@pytest.mark.parametrize("host", ["localhost:8080", "127.0.0.1:8080", "[::1]:8080"])
def test_warning_when_the_address_is_this_machine(ready_client, article_id, host):
    page = ready_client.get(f"/articles/{article_id}", headers={"host": host}).text
    assert html.unescape(en("qr.local_warning")) in html.unescape(page)
    assert 'href="/settings?tab=general"' in page


def test_no_warning_when_the_setting_names_a_reachable_address(ready_client, article_id):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "qr_base_url", BASE, commit=False)
    page = ready_client.get(f"/articles/{article_id}", headers={"host": "localhost:8080"}).text
    assert en("qr.local_warning") not in page


# ---- the setting ----


def test_settings_page_has_the_card_on_the_general_tab(ready_client):
    page = ready_client.get("/settings").text
    assert 'action="/settings/qr"' in page and 'name="qr_base_url"' in page
    assert en("settings.qr_address") in page


def test_settings_page_shows_the_stored_address(ready_client):
    save(ready_client, BASE)
    assert f'value="{BASE}"' in ready_client.get("/settings").text


def test_save_stores_the_normalised_address_and_returns_to_the_tab(ready_client):
    response = save(ready_client, f"  {BASE}/  ")
    assert response.status_code == 303 and response.headers["location"] == "/settings?tab=general"
    assert stored(ready_client) == BASE


def test_an_empty_field_removes_the_address(ready_client):
    save(ready_client, BASE)
    assert save(ready_client, "   ").status_code == 303
    assert stored(ready_client) is None


@pytest.mark.parametrize(
    "bad", ["shelf.example.com", "ftp://shelf.example.com", "https://u:p@shelf.example.com", "https://h/?q=1", "https://h/#x"]
)
def test_a_bad_address_saves_nothing_and_keeps_what_was_typed(ready_client, bad):
    save(ready_client, BASE)
    response = save(ready_client, bad)
    assert response.status_code == 200
    assert en("settings.invalid_qr_base_url") in html.unescape(response.text)
    assert html.escape(bad) in response.text or html.escape(bad, quote=False) in response.text
    assert stored(ready_client) == BASE
    assert 'action="/settings/qr"' in response.text  # the general tab, where the card is


def test_the_post_needs_a_session_and_a_token(ready_client):
    token = csrf_of(ready_client, "/settings")
    assert ready_client.post("/settings/qr", data={"qr_base_url": BASE}).status_code == 403
    assert stored(ready_client) is None
    ready_client.cookies.clear()
    assert ready_client.post("/settings/qr", data={"csrf_token": token, "qr_base_url": BASE}).status_code == 403
    assert stored(ready_client) is None


def test_a_backup_with_the_address_is_accepted_and_one_with_a_bad_address_is_not(tmp_path):
    """The restore checks every stored setting with its validator: a stored address passes, a value
    this version could not have saved (credentials in it) makes the file unusable."""
    import json
    import sqlite3

    from reprintbuddy.db import make_engine, upgrade
    from reprintbuddy.ops import fullbackup

    path = tmp_path / "backup.db"
    upgrade(make_engine(path))

    def store(value):
        con = sqlite3.connect(path)
        con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('qr_base_url', ?)", (json.dumps(value),))
        con.commit()
        con.close()

    store(BASE)
    fullbackup._check_settings(path)  # no exception
    store("https://user:secret@shelf.example.com")
    with pytest.raises(fullbackup.Incompatible):
        fullbackup._check_settings(path)
