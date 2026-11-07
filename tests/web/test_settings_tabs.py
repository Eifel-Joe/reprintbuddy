"""The settings page in tabs: one tab per topic, cards in a grid, links work without scripts."""

import re

import pytest
from conftest import PASSWORD, auth_me, csrf_of

from reprintbuddy.web.routes.settings import SECTION_TAB, SETTINGS_TABS


def post(client, path, **data):
    token = csrf_of(client, "/settings")
    return client.post(path, data={"csrf_token": token, **data})


def tab_links(page):
    return re.findall(r'<a class="tab( active)?" href="([^"]+)"', page)


def card_forms(page):
    return re.findall(r'<form id="(settings-[a-z]+)"', page)


def test_the_tab_table_has_the_agreed_shape():
    assert SETTINGS_TABS == [
        ("general", "settings", "settings.tab.general"),
        ("bambuddy", "link", "settings.tab.bambuddy"),
        ("account", "user", "settings.tab.account"),
        ("maintenance", "download", "settings.tab.maintenance"),
        ("automation", "list-ordered", "settings.tab.automation"),
    ]
    assert SECTION_TAB == {
        "language": "general",
        "server": "general",
        "bambuddy": "bambuddy",
        "password": "account",
        "automation": "automation",
    }


def test_five_tabs_general_active_with_its_cards(ready_client):
    page = ready_client.get("/settings").text
    assert tab_links(page) == [
        (" active", "/settings?tab=general"),
        ("", "/settings?tab=bambuddy"),
        ("", "/settings?tab=account"),
        ("", "/settings?tab=maintenance"),
        ("", "/settings?tab=automation"),
    ]
    assert 'aria-current="page"' in page
    assert card_forms(page) == ["settings-language", "settings-server"]
    assert 'id="ops-restart"' in page
    assert 'action="/settings/password"' not in page
    assert 'action="/settings/bambuddy"' not in page
    assert 'id="ops-update"' not in page


def test_each_tab_shows_only_its_cards(ready_client):
    account = ready_client.get("/settings?tab=account").text
    assert card_forms(account) == ["settings-password"]
    assert 'action="/settings/language"' not in account
    bambuddy = ready_client.get("/settings?tab=bambuddy").text
    assert card_forms(bambuddy) == ["settings-bambuddy"]
    maintenance = ready_client.get("/settings?tab=maintenance").text
    assert 'id="ops-update"' in maintenance and 'id="ops-export"' in maintenance
    assert 'id="ops-restart"' not in maintenance


@pytest.mark.parametrize("query", ["?tab=unknown", "?tab=", "?tab=server", "?tab=ACCOUNT"])
def test_an_unknown_tab_opens_general(ready_client, query):
    page = ready_client.get("/settings" + query).text
    assert tab_links(page)[0] == (" active", "/settings?tab=general")
    assert card_forms(page) == ["settings-language", "settings-server"]


def test_a_wrong_password_reopens_the_account_tab_with_the_error(ready_client):
    response = post(
        ready_client,
        "/settings/password",
        current_password="not-my-password",
        new_password="brand-new-pass1",
        new_password_repeat="brand-new-pass1",
    )
    assert response.status_code == 200
    assert tab_links(response.text)[2] == (" active", "/settings?tab=account")
    assert "current password is wrong" in response.text
    assert card_forms(response.text) == ["settings-password"]


def test_a_failed_server_save_reopens_general(ready_client):
    response = post(ready_client, "/settings/server", host="0.0.0.0", port="abc")
    assert response.status_code == 200
    assert tab_links(response.text)[0][0] == " active"
    assert "port must be a number" in response.text


def test_a_failed_bambuddy_save_reopens_its_tab(ready_client):
    response = post(ready_client, "/settings/bambuddy", bambuddy_address="")
    assert response.status_code == 200
    assert tab_links(response.text)[1] == (" active", "/settings?tab=bambuddy")
    assert "Please fill in" in response.text


def test_saving_the_language_redirects_to_general(ready_client):
    response = post(ready_client, "/settings/language", language="de")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=general"


def test_saving_the_server_redirects_to_general(ready_client):
    response = post(ready_client, "/settings/server", host="127.0.0.1", port="9090")
    assert response.headers["location"] == "/settings?tab=general"


def test_saving_bambuddy_redirects_to_its_tab(ready_client, respx_mock):
    respx_mock.get("http://bambuddy.test:8000/api/v1/auth/me").respond(200, json=auth_me())
    response = post(
        ready_client, "/settings/bambuddy", bambuddy_address="http://bambuddy.test:8000", bambuddy_token=""
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=bambuddy"


def test_changing_the_password_redirects_to_the_account_tab(ready_client):
    response = post(
        ready_client,
        "/settings/password",
        current_password=PASSWORD,
        new_password="brand-new-pass1",
        new_password_repeat="brand-new-pass1",
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=account"


def test_the_save_button_sits_in_the_card_head_and_points_at_its_form(ready_client):
    general = ready_client.get("/settings").text
    for section in ("language", "server"):
        assert f'<button class="btn" type="submit" form="settings-{section}">' in general, section
        card = general[general.index(f'<form id="settings-{section}"') :]
        assert "card-head" in general[: general.index(f'<form id="settings-{section}"')]
        assert "<button" not in card[: card.index("</form>")], section  # the button is not inside the form
    for tab, section in (("account", "password"), ("bambuddy", "bambuddy")):
        page = ready_client.get(f"/settings?tab={tab}").text
        assert f'<button class="btn" type="submit" form="settings-{section}">' in page
        assert page.index('class="card-head"') < page.index(f'form="settings-{section}"') < page.index(
            f'<form id="settings-{section}"'
        )


def test_tls_remove_is_a_switch_and_still_posts_on(ready_client, tls_pair):
    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    page = ready_client.get("/settings").text
    tag = re.search(r'<input[^>]*name="tls_remove"[^>]*>', page).group(0)
    assert 'class="switch"' in tag and 'role="switch"' in tag
    post(ready_client, "/settings/server", host="0.0.0.0", port="8080", tls_remove="on")
    assert "tls_remove" not in ready_client.get("/settings").text


def test_the_pem_fields_sit_under_their_label_across_the_card(ready_client):
    page = ready_client.get("/settings").text
    for name in ("tls_cert", "tls_key"):
        row = re.search(r'<div class="setting-row stack">((?:(?!</form>).)*?<textarea id="%s" name="%s".*?)</div>\s*</div>' % (name, name), page, re.S)
        assert row, name
        assert f'name="{name}_file"' in row.group(1)
    assert page.count('type="file"') == 2


def test_the_setup_guides_lead_back_to_the_bambuddy_tab(ready_client):
    for path in ("/setup/bambuddy/user", "/setup/bambuddy/key"):
        assert 'href="/settings?tab=bambuddy"' in ready_client.get(path).text, path


def test_restart_failure_lands_on_general(ops_env):
    from reprintbuddy.ops.runner import CommandResult
    from test_ops_restart import RESTART_CMD, post_restart

    ops_env.runner.on(RESTART_CMD, CommandResult(1, stderr="sudo: a password is required"))
    response = post_restart(ops_env)
    assert response.headers["location"] == "/settings?tab=general&ops=restart_failed#ops-restart"
    page = ops_env.client.get(response.headers["location"].split("#")[0]).text
    assert "Restart failed" in page and 'id="ops-restart"' in page


def test_update_answers_land_on_the_maintenance_tab(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    response = ops_env.client.post("/settings/update/check", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings?tab=maintenance")
    assert response.headers["location"].endswith("#ops-update")


def test_a_page_without_error_has_exactly_one_grid(ready_client):
    for tab, _icon, _label in SETTINGS_TABS:
        assert ready_client.get(f"/settings?tab={tab}").text.count('class="settings-grid"') == 1, tab


def test_secret_fields_stay_empty_and_every_form_has_a_csrf_token(ready_client, tls_pair):
    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    for tab, _icon, _label in SETTINGS_TABS:
        page = ready_client.get(f"/settings?tab={tab}").text
        assert page.count('name="csrf_token"') >= page.count("<form"), tab
    bambuddy = ready_client.get("/settings?tab=bambuddy").text
    token = re.search(r'<input[^>]*name="bambuddy_token"[^>]*>', bambuddy).group(0)
    assert 'value=""' in token and 'placeholder="' in token
