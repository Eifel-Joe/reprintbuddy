"""Rights after an update: the first page after a start checks the stored key (Spec 11.3)."""

import httpx
import pytest
from conftest import ADMIN, PASSWORD, REQUIRED, auth_me, csrf_of, finish_bambuddy_setup, mock_auth_me

from reprintbuddy import settings as st

URL = "http://bambuddy.test:8000"
ME = f"{URL}/api/v1/auth/me"
WITHOUT_INVENTORY = [right for right in REQUIRED if right != "inventory:read"]


@pytest.fixture
def rights_client(admin_client):
    """Setup done, but this start has not checked the key's rights yet."""
    client = finish_bambuddy_setup(admin_client)
    client.app.state.rights.status = "unknown"
    return client


def test_first_page_checks_rights_once(rights_client, respx_mock):
    route = mock_auth_me(respx_mock)
    assert rights_client.get("/").status_code == 200
    assert rights_client.get("/").status_code == 200
    assert route.call_count == 1


def test_login_page_does_not_check(rights_client, respx_mock):
    route = mock_auth_me(respx_mock)
    rights_client.cookies.clear()
    assert rights_client.get("/login").status_code == 200
    assert route.call_count == 0


def test_unreachable_bambuddy_shows_no_notice(rights_client, respx_mock):
    respx_mock.get(ME).mock(side_effect=httpx.ConnectError("down"))
    page = rights_client.get("/")
    assert page.status_code == 200
    assert "rights-dialog" not in page.text
    assert "rights-banner" not in page.text


def test_stored_credentials_only_after_the_setup(admin_client):
    from reprintbuddy.web.deps import stored_credentials

    assert stored_credentials(admin_client.app) is None  # step 2 of the setup still open
    finish_bambuddy_setup(admin_client)
    assert stored_credentials(admin_client.app) == (URL, "bb_testkey")


# --- the notice -----------------------------------------------------------------------------


@pytest.fixture
def missing_client(rights_client, respx_mock):
    """This start finds `inventory:read` missing at the key's owner."""
    mock_auth_me(respx_mock, permissions=WITHOUT_INVENTORY)
    return rights_client


def test_missing_right_shows_dialog_with_place_and_guide(missing_client):
    page = missing_client.get("/")
    assert page.status_code == 200
    html = page.text
    assert 'class="rights-dialog"' in html and 'role="alertdialog"' in html
    assert "Bambuddy right missing" in html
    assert "Read Inventory" in html and "inventory:read" in html
    assert 'href="/setup/bambuddy/user"' in html and 'href="/setup/bambuddy/key"' in html
    assert 'action="/bambuddy/rights/recheck"' in html and "Check again" in html
    assert 'action="/bambuddy/rights/later"' in html and "Later" in html
    assert "bb_testkey" not in html  # the key is never part of a page
    assert "rights-banner" not in html


def test_dialog_is_on_every_page_of_the_interface(missing_client):
    for path in ("/", "/settings", "/articles"):
        assert "rights-dialog" in missing_client.get(path).text, path


def test_all_rights_show_nothing(rights_client, respx_mock):
    mock_auth_me(respx_mock)
    html = rights_client.get("/").text
    assert "rights-dialog" not in html and "rights-banner" not in html


def test_notice_in_german(missing_client):
    with missing_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    html = missing_client.get("/").text
    assert "Bambuddy-Recht fehlt" in html
    assert "Erneut prüfen" in html and "Später" in html
    assert "Read Inventory" in html  # Bambuddy shows its rights in English only


def test_forms_carry_the_page_to_come_back_to(missing_client):
    html = missing_client.get("/articles?q=x").text
    assert html.count('name="next" value="/articles?q=x"') == 2  # recheck and later


# --- check again, later, and feedback from settings and setup ---------------------------------

RECHECK, LATER = "/bambuddy/rights/recheck", "/bambuddy/rights/later"


def _post(client, path, token=None, **data):
    token = token if token is not None else csrf_of(client, "/")
    return client.post(path, data={"csrf_token": token, **data})


def _everything_granted(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me())


def test_recheck_after_fix_clears_notice(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)  # the person added the right in Bambuddy
    response = _post(missing_client, RECHECK, next="/articles")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    page = missing_client.get("/articles").text
    assert "rights-dialog" not in page and "rights-banner" not in page
    assert "Bambuddy: all rights are there." in page


def test_recheck_still_missing_keeps_dialog(missing_client):
    assert "rights-dialog" in missing_client.get("/").text
    response = _post(missing_client, RECHECK, next="/")
    assert response.status_code == 303
    assert "rights-dialog" in missing_client.get("/").text


def test_recheck_with_bambuddy_down_says_so_and_keeps_the_dialog(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    respx_mock.get(ME).mock(side_effect=httpx.ConnectError("down"))
    _post(missing_client, RECHECK, next="/")
    page = missing_client.get("/").text
    assert "rights-dialog" in page
    assert "Bambuddy" in page and 'class="flash"' in page  # one line says why nothing changed


def test_later_switches_to_banner(missing_client):
    assert "rights-dialog" in missing_client.get("/").text
    response = _post(missing_client, LATER, next="/articles")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    page = missing_client.get("/articles").text
    assert 'class="rights-banner"' in page and "rights-dialog" not in page
    assert "A Bambuddy right is missing: Read Inventory." in page
    assert 'href="/settings"' in page and 'action="/bambuddy/rights/recheck"' in page


def test_later_survives_pages_not_a_new_login(missing_client):
    assert "rights-dialog" in missing_client.get("/").text
    _post(missing_client, LATER, next="/")
    for path in ("/", "/settings", "/articles"):
        assert "rights-banner" in missing_client.get(path).text, path
    _post(missing_client, "/logout")
    token = csrf_of(missing_client, "/login")
    login = missing_client.post("/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD})
    assert login.status_code == 303
    page = missing_client.get("/").text
    assert "rights-dialog" in page and "rights-banner" not in page


@pytest.mark.parametrize("path", [RECHECK, LATER])
def test_rights_posts_need_csrf(missing_client, path):
    assert missing_client.post(path, data={"next": "/"}).status_code == 403
    assert missing_client.post(path, data={"csrf_token": "wrong", "next": "/"}).status_code == 403


@pytest.mark.parametrize("path", [RECHECK, LATER])
def test_rights_posts_need_a_login(missing_client, path):
    token = csrf_of(missing_client, "/")
    missing_client.cookies.clear()
    assert missing_client.post(path, data={"csrf_token": token}).status_code == 403


@pytest.mark.parametrize("path", [RECHECK, LATER])
@pytest.mark.parametrize("target", ["https://example.com/", "//example.com/", "/\\example.com", ""])
def test_next_must_be_local(missing_client, path, target):
    response = _post(missing_client, path, next=target)
    assert response.status_code == 303 and response.headers["location"] == "/"


def test_saving_settings_with_full_rights_clears_notice(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)
    token = csrf_of(missing_client, "/settings")
    response = missing_client.post(
        "/settings/bambuddy", data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": "bb_newkey123"}
    )
    assert response.status_code == 303
    page = missing_client.get("/articles").text
    assert "rights-dialog" not in page and "rights-banner" not in page


def test_setup_with_full_rights_marks_the_key_as_checked(admin_client, respx_mock):
    mock_auth_me(respx_mock)
    assert admin_client.app.state.rights.status == "unknown"
    token = csrf_of(admin_client, "/setup/bambuddy/connect")
    response = admin_client.post(
        "/setup/bambuddy/connect",
        data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": "bb_SETUPKEY_123456"},
    )
    assert response.status_code == 303
    assert admin_client.app.state.rights.status == "ok"
