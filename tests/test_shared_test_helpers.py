"""The helpers many test files share instead of writing them out each (refactoring before 1.0.0, R2)."""

from __future__ import annotations

import json

from conftest import ADMIN, login
from reprintbuddy.i18n import translate
from reprintbuddy.ops import fullbackup
from texts import de, en, en_html


def _key_with_an_apostrophe() -> str:
    from reprintbuddy import i18n

    catalog = json.loads((i18n.LOCALES_DIR / "en.json").read_text(encoding="utf-8"))

    def walk(node, prefix=""):
        for key, value in node.items():
            full = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                yield from walk(value, full)
            elif "'" in value and "{" not in value:
                yield full

    return next(walk(catalog))


def test_en_is_the_text_and_en_html_the_text_as_the_page_carries_it():
    key = _key_with_an_apostrophe()
    assert en(key) == translate("en", key)
    assert "'" in en(key)
    assert "&#39;" in en_html(key) and "'" not in en_html(key)


def test_de_is_the_german_text():
    assert de("nav.articles") == translate("de", "nav.articles")


def test_login_posts_the_admin_by_default(ready_client):
    ready_client.cookies.clear()  # signed out: a new visitor with the admin's password
    response = login(ready_client)
    assert response.status_code == 303
    assert ready_client.get("/").status_code == 200
    assert login(ready_client, "wrong-password", username=ADMIN).status_code != 303


def test_fast_kdf_is_cheap(fast_kdf):
    assert fullbackup.KDF.time_cost == 1


def test_session_is_on_a_migrated_database(session):
    from reprintbuddy import settings as st

    assert st.get_setting(session, "automation") is not None
