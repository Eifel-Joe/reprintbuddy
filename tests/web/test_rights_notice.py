"""Rights after an update: the first page after a start checks the stored key (Spec 11.3)."""

import re

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


def test_update_adds_a_right_shows_dialog(rights_client, respx_mock):
    """The case this is built for: a key made for the version before needs `filaments:read` now."""
    mock_auth_me(respx_mock, permissions=[p for p in REQUIRED if p != "filaments:read"])
    html = rights_client.get("/").text
    assert "rights-dialog" in html
    assert "Read Filaments" in html and "filaments:read" in html
    assert "Group right" in html  # where it can be added
    assert "Read Inventory" in html and "inventory:read" in html  # the others are listed, as granted
    assert html.count('aria-label="missing"') == 1 and html.count('aria-label="granted"') == 6


def test_the_guide_links_of_the_dialog_lead_to_the_guide(missing_client):
    """C1 of the review: the links must not end in a loop back to the dialog."""
    html = missing_client.get("/").text
    dialog = html[html.index('class="rights-dialog"') :]
    links = sorted(set(re.findall(r'href="(/setup/bambuddy/[a-z]+)"', dialog)))
    assert links == ["/setup/bambuddy/key", "/setup/bambuddy/user"]
    for link in links:
        page = missing_client.get(link)
        assert page.status_code == 200, link
        assert "rights-dialog" not in page.text and "rights-banner" not in page.text, link
        assert "Read Filaments" in page.text or "Read Status" in page.text, link
        assert 'href="/settings?tab=bambuddy"' in page.text, link  # the way back


def test_details_link_leads_to_details(missing_client):
    """I2 of the review: after "Later" the settings page still says which right, where, and the guide."""
    _post(missing_client, LATER, next="/")
    page = missing_client.get("/")
    assert 'href="/settings?tab=bambuddy#bambuddy"' in page.text  # the link of the warning line
    settings = missing_client.get("/settings?tab=bambuddy").text
    assert 'id="bambuddy"' in settings
    section = settings[settings.index('id="bambuddy"') : settings.index('action="/settings/bambuddy"')]
    assert "<code>inventory:read</code>" in section and "Read Inventory" in section
    assert "Group right" in section and "Key right" in section  # where it can be missing
    assert 'href="/setup/bambuddy/user"' in section and 'href="/setup/bambuddy/key"' in section


def test_settings_show_no_rights_list_when_nothing_is_missing(rights_client, respx_mock):
    mock_auth_me(respx_mock)
    section = rights_client.get("/settings?tab=bambuddy").text
    assert "<code>inventory:read</code>" not in section


def _save_settings_with_the_right_still_missing(client, **headers):
    """A page that is the answer to a POST: the failed save of the Bambuddy settings."""
    token = csrf_of(client, "/settings")
    return client.post(
        "/settings/bambuddy",
        data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": "bb_newkey123"},
        headers=headers,
    )


def _nexts(html):
    return re.findall(r'name="next" value="([^"]*)"', html)


def test_next_on_the_answer_to_a_post_is_the_page_the_form_came_from(missing_client):
    """I1 of the review: never the POST route, which answers a GET with 405."""
    _post(missing_client, LATER, next="/")
    response = _save_settings_with_the_right_still_missing(missing_client, Referer="http://testserver/settings")
    assert response.status_code == 200 and "rights-banner" in response.text
    assert set(_nexts(response.text)) == {"/settings"}
    back = _post(missing_client, RECHECK, next=_nexts(response.text)[0])
    assert back.status_code == 303
    assert missing_client.get(back.headers["location"]).status_code == 200


@pytest.mark.parametrize("referer", [None, "http://evil.example/settings", "not a url", "http://testserver"])
def test_next_on_the_answer_to_a_post_without_a_usable_referer_is_the_start_page(missing_client, referer):
    _post(missing_client, LATER, next="/")
    headers = {"Referer": referer} if referer else {}
    response = _save_settings_with_the_right_still_missing(missing_client, **headers)
    assert set(_nexts(response.text)) == {"/"}


def test_next_on_a_page_shown_by_get_is_that_page(missing_client):
    assert set(_nexts(missing_client.get("/settings?x=1").text)) == {"/settings?x=1"}


def _other_right_goes_missing(client):
    """A later start finds one more right missing: a new verdict about the rights."""
    from reprintbuddy.bambuddy.connection import ConnectionCheck

    client.app.state.rights.record(
        ConnectionCheck(False, "missing_permissions", owner="reprintbuddy", missing=("inventory:read", "filaments:read"))
    )


def test_later_holds_only_for_the_rights_it_was_pressed_for(missing_client):
    """M1 of the review: another right missing on a later start brings the dialog back."""
    assert "rights-dialog" in missing_client.get("/").text
    _post(missing_client, LATER, next="/")
    assert "rights-banner" in missing_client.get("/").text
    _other_right_goes_missing(missing_client)
    page = missing_client.get("/").text
    assert "rights-dialog" in page and "rights-banner" not in page
    assert "Read Filaments" in page


def test_later_is_forgotten_when_a_save_brings_every_right(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    _post(missing_client, LATER, next="/")
    _everything_granted(respx_mock)
    token = csrf_of(missing_client, "/settings")
    saved = missing_client.post(
        "/settings/bambuddy", data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": "bb_newkey123"}
    )
    assert saved.status_code == 303
    assert "rights-banner" not in missing_client.get("/").text
    mock_auth_me(respx_mock, permissions=WITHOUT_INVENTORY)
    missing_client.app.state.rights.status = "unknown"  # the next start finds the same right missing
    page = missing_client.get("/").text
    assert "rights-dialog" in page and "rights-banner" not in page


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

RECHECK, LATER, DONE = "/bambuddy/rights/recheck", "/bambuddy/rights/later", "/bambuddy/rights/done"
ALL_THERE = "Bambuddy: all rights are there."


def _post(client, path, token=None, **data):
    token = token if token is not None else csrf_of(client, "/")
    return client.post(path, data={"csrf_token": token, **data})


def _everything_granted(respx_mock):
    respx_mock.get(ME).respond(200, json=auth_me())


def _paragraph_with(page, text):
    """The whole opening tag of the <p> that holds `text`."""
    end = page.index(text)
    return page[page.rindex("<p", 0, end) : end]


def _recheck_from_the_dialog(client, next="/"):
    """What the "Check again" button of the dialog sends."""
    return _post(client, RECHECK, next=next, **{"from": "dialog"})


def _dialog(page):
    return page[page.index('class="rights-dialog"') :]


def _rights_listed(dialog):
    """Every right listed in the dialog: its code and whether it is ticked ("ok") or crossed ("bad")."""
    items = re.findall(r'<li class="perm (\w+)">(.*?)</li>', dialog, re.S)
    return {re.search(r"<code>([^<]+)</code>", body).group(1): state for state, body in items}


def test_recheck_after_fix_turns_the_dialog_green(missing_client, respx_mock):
    """p31: a successful check from the dialog keeps it open, now green, instead of closing it."""
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)  # the person added the right in Bambuddy
    response = _recheck_from_the_dialog(missing_client, next="/articles")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    page = missing_client.get("/articles").text
    assert "rights-dialog" in page and "rights-banner" not in page
    assert "Bambuddy rights complete" in page
    assert 'aria-label="missing"' not in page and "Bambuddy right missing" not in page


def test_the_check_again_button_of_the_dialog_says_it_comes_from_the_dialog(missing_client):
    dialog = _dialog(missing_client.get("/").text)
    recheck = dialog[dialog.index('action="/bambuddy/rights/recheck"') : dialog.index('action="/bambuddy/rights/later"')]
    assert 'name="from" value="dialog"' in recheck


def test_recheck_success_keeps_the_dialog_with_every_right_ticked(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)
    _recheck_from_the_dialog(missing_client)
    page = missing_client.get("/articles?q=x").text
    dialog = _dialog(page)
    # Good news, not an alert: a screen reader announces it as a plain dialog.
    assert 'role="dialog"' in dialog and "alertdialog" not in dialog
    assert _rights_listed(dialog) == {code: "ok" for code in REQUIRED}
    assert "perm bad" not in page
    assert dialog.count('aria-label="granted"') == len(REQUIRED)
    assert ALL_THERE in dialog and page.count(ALL_THERE) == 1  # the text of the dialog, not a message too
    assert "Read Inventory" in dialog and "inventory:read" in dialog
    # One button: Continue, back to this very page. No "Later", nothing to check again.
    assert 'action="/bambuddy/rights/done"' in dialog and "Continue" in dialog
    done = dialog[dialog.index('action="/bambuddy/rights/done"') :]
    assert 'name="next" value="/articles?q=x"' in done and 'name="csrf_token"' in done
    assert "autofocus" in done
    assert "Later" not in dialog and 'action="/bambuddy/rights/later"' not in dialog
    assert 'action="/bambuddy/rights/recheck"' not in dialog


def test_only_the_success_dialog_is_marked_green(missing_client, respx_mock):
    """The frame of the dialog follows the result: red while a right is missing, green after."""
    assert 'class="rights-overlay"' in missing_client.get("/").text
    _everything_granted(respx_mock)
    _recheck_from_the_dialog(missing_client)
    assert 'class="rights-overlay rights-ok"' in missing_client.get("/").text


def test_success_dialog_stays_until_continue(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)
    _recheck_from_the_dialog(missing_client)
    for path in ("/settings", "/articles"):
        page = missing_client.get(path).text
        assert "rights-dialog" in page and "Bambuddy rights complete" in page, path


def test_continue_closes_the_success_dialog(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)
    _recheck_from_the_dialog(missing_client)
    assert "rights-dialog" in missing_client.get("/").text
    response = _post(missing_client, DONE, next="/articles")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    for path in ("/articles", "/"):
        page = missing_client.get(path).text
        assert "rights-dialog" not in page and "rights-banner" not in page, path
        assert ALL_THERE not in page, path


@pytest.fixture
def green_left_open(missing_client, respx_mock):
    """The green dialog is shown, and the person closes the tab without "Continue"."""
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)
    _recheck_from_the_dialog(missing_client)
    assert "Bambuddy rights complete" in missing_client.get("/").text
    return missing_client


def _restart(client):
    """A new process (an update, say): the rights are unknown again; the session cookie stays."""
    from reprintbuddy.web.rights import RightsMonitor

    client.app.state.rights = RightsMonitor()


def test_a_right_missing_again_outranks_the_green_dialog(green_left_open):
    _other_right_goes_missing(green_left_open)
    page = green_left_open.get("/").text
    assert "Bambuddy right missing" in page and "Bambuddy rights complete" not in page
    assert "rights-ok" not in page


def test_green_dialog_left_open_claims_nothing_after_a_restart_with_bambuddy_down(green_left_open, respx_mock):
    """Spec 11.3: while Bambuddy cannot be reached after a start, nothing is shown."""
    _restart(green_left_open)
    respx_mock.get(ME).mock(side_effect=httpx.ConnectError("down"))
    page = green_left_open.get("/").text
    assert "rights-dialog" not in page and ALL_THERE not in page


def test_green_dialog_left_open_does_not_come_back_from_the_banner(green_left_open, respx_mock):
    """After a restart with a right missing, "Later", then "Check again" in the warning line: a message, no dialog."""
    _restart(green_left_open)
    mock_auth_me(respx_mock, permissions=WITHOUT_INVENTORY)
    assert "Bambuddy right missing" in green_left_open.get("/").text
    _post(green_left_open, LATER, next="/")
    _everything_granted(respx_mock)
    _post(green_left_open, RECHECK, next="/")  # from the warning line: no `from`
    page = green_left_open.get("/").text
    assert "rights-dialog" not in page and page.count(ALL_THERE) == 1


def test_green_dialog_left_open_does_not_cover_the_settings_after_a_save(green_left_open, respx_mock):
    _restart(green_left_open)
    mock_auth_me(respx_mock, permissions=WITHOUT_INVENTORY)
    assert "Bambuddy right missing" in green_left_open.get("/").text
    _everything_granted(respx_mock)
    token = csrf_of(green_left_open, "/settings")
    saved = green_left_open.post(
        "/settings/bambuddy", data={"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": "bb_newkey123"}
    )
    assert saved.status_code == 303
    assert "rights-dialog" not in green_left_open.get("/settings").text


def test_recheck_from_the_banner_shows_no_dialog(missing_client, respx_mock):
    """After "Later" there is no dialog to keep open: the good news is a message, as before."""
    assert "rights-dialog" in missing_client.get("/").text
    _post(missing_client, LATER, next="/")
    banner = missing_client.get("/").text
    banner = banner[banner.index('class="rights-banner"') :]
    assert 'name="from"' not in banner[: banner.index("</form>")]
    _everything_granted(respx_mock)
    response = _post(missing_client, RECHECK, next="/articles")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    page = missing_client.get("/articles").text
    assert "rights-dialog" not in page and "rights-banner" not in page
    assert ALL_THERE in page
    # Good news keeps the green look; only trouble gets the warning look.
    tag = _paragraph_with(page, ALL_THERE)
    assert 'class="flash"' in tag and 'role="status"' in tag and "alert" not in tag


def test_success_dialog_in_german(missing_client, respx_mock):
    with missing_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    assert "rights-dialog" in missing_client.get("/").text
    _everything_granted(respx_mock)
    _recheck_from_the_dialog(missing_client)
    dialog = _dialog(missing_client.get("/").text)
    assert "Bambuddy-Rechte vollständig" in dialog and "Weiter" in dialog
    assert "Bambuddy: alle Rechte sind da." in dialog
    assert "Read Inventory" in dialog  # Bambuddy shows its rights in English only


def test_recheck_still_missing_keeps_dialog_and_says_so(missing_client):
    """M5 of the review: the person sees that Bambuddy was asked again."""
    assert "rights-dialog" in missing_client.get("/").text
    response = _post(missing_client, RECHECK, next="/")
    assert response.status_code == 303
    page = missing_client.get("/").text
    assert "rights-dialog" in page
    message = "Checked again: Bambuddy still reports a missing right."
    # The overlay covers the page: the message must stand inside the dialog, and only there.
    assert message in page[page.index('class="rights-dialog"') :]
    assert page.count(message) == 1
    # Not the green success box: the right is still missing, so it is a warning.
    tag = _paragraph_with(page, message)
    assert 'class="alert"' in tag and 'role="alert"' in tag and "flash" not in tag


def test_recheck_still_missing_says_so_in_german(missing_client):
    with missing_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    assert "rights-dialog" in missing_client.get("/").text
    _post(missing_client, RECHECK, next="/")
    assert "Erneut geprüft: Bambuddy meldet weiterhin ein fehlendes Recht." in missing_client.get("/").text


def test_the_dialog_puts_the_focus_on_check_again(missing_client):
    """M6 of the review: without JavaScript, so a keyboard user starts in the dialog."""
    html = missing_client.get("/").text
    dialog = html[html.index('class="rights-dialog"') :]
    recheck = dialog[dialog.index('action="/bambuddy/rights/recheck"') : dialog.index('action="/bambuddy/rights/later"')]
    later = dialog[dialog.index('action="/bambuddy/rights/later"') :]
    assert "autofocus" in recheck and "autofocus" not in later


def test_recheck_with_bambuddy_down_says_so_and_keeps_the_dialog(missing_client, respx_mock):
    assert "rights-dialog" in missing_client.get("/").text
    respx_mock.get(ME).mock(side_effect=httpx.ConnectError("down"))
    _post(missing_client, RECHECK, next="/")
    page = missing_client.get("/").text
    assert "rights-dialog" in page
    message = "Could not reach Bambuddy at this address."
    assert message in page  # one line says why nothing changed
    tag = _paragraph_with(page, message)  # nothing could be checked: not a success either
    assert 'class="alert"' in tag and 'role="alert"' in tag and "flash" not in tag


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


@pytest.mark.parametrize("path", [RECHECK, LATER, DONE])
def test_rights_posts_need_csrf(missing_client, path):
    assert missing_client.post(path, data={"next": "/"}).status_code == 403
    assert missing_client.post(path, data={"csrf_token": "wrong", "next": "/"}).status_code == 403


@pytest.mark.parametrize("path", [RECHECK, LATER, DONE])
def test_rights_posts_need_a_login(missing_client, respx_mock, path):
    """With a valid CSRF token in the cookie, so that only the missing login can be the reason."""
    token = csrf_of(missing_client, "/")
    cookie = missing_client.cookies.get("reprintbuddy_session")
    _post(missing_client, "/logout", token=token)  # ends the session on the server
    missing_client.cookies.set("reprintbuddy_session", cookie)  # the old cookie, token and all
    calls = respx_mock.calls.call_count
    response = missing_client.post(path, data={"csrf_token": token, "next": "/"})
    assert response.status_code == 403
    assert respx_mock.calls.call_count == calls  # nothing asked Bambuddy


def test_a_post_does_not_start_the_check(rights_client, respx_mock):
    """Only a page shown for a GET starts the check of the start."""
    route = mock_auth_me(respx_mock)
    token = csrf_of(rights_client, "/login")  # a page that does not start the check itself
    assert _post(rights_client, "/settings/language", token=token, language="en").status_code == 303
    assert route.call_count == 0 and rights_client.app.state.rights.status == "unknown"


@pytest.mark.parametrize("path", [RECHECK, LATER, DONE])
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
    assert response.status_code == 303  # on to the page with the result (p179, p192)
    assert admin_client.app.state.rights.status == "ok"


# --- p109 (v0.3.7): group right or key right, and when a new key is needed --------------------
# Bambuddy's interface only creates and deletes keys; the rights of an existing key cannot be
# changed. A right missing in the group is added there, a right missing at the key needs a new key.

SPEC = {
    "en": (
        "does not let you change the rights of an existing key",
        "you do not need a new key",
    ),
    "de": (
        "die Rechte eines vorhandenen Schlüssels nicht ändern lässt",
        "ein neuer Schlüssel ist nicht nötig",
    ),
}
KIND = {"en": ("Group right", "Key right"), "de": ("Gruppenrecht", "Schlüsselrecht")}


def _use_language(client, language):
    with client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", language)


@pytest.mark.parametrize("language", ["en", "de"])
def test_dialog_says_group_right_key_right_and_when_a_new_key_is_needed(missing_client, language):
    _use_language(missing_client, language)
    dialog = missing_client.get("/").text
    dialog = dialog[dialog.index('class="rights-dialog"') :]
    for phrase in SPEC[language]:
        assert phrase in dialog, (language, phrase)
    group, key = KIND[language]
    line = dialog[dialog.index('class="where"') :]
    line = line[: line.index("</span>")]
    assert group in line and key in line  # per missing right: which one is a group right, which a key right
    assert "Read Status" in line  # inventory:read is covered at the key by "Read Status"
    assert 'href="/setup/bambuddy/user"' in line and 'href="/setup/bambuddy/key"' in line


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_intro_no_longer_promises_that_no_new_key_is_ever_needed(missing_client, language):
    _use_language(missing_client, language)
    html = missing_client.get("/").text
    intro = re.search(r'<p id="rights-intro">(.*?)</p>', html, re.S).group(1)
    assert "new key" not in intro and "neuen Schlüssel" not in intro and "neuer Schlüssel" not in intro


def test_a_missing_queue_right_names_the_key_right_manage_queue(rights_client, respx_mock):
    mock_auth_me(respx_mock, permissions=[p for p in REQUIRED if p != "queue:create"])
    line = rights_client.get("/").text
    line = line[line.index('class="where"') :]
    assert "Manage Queue" in line[: line.index("</span>")]


@pytest.mark.parametrize("language", ["en", "de"])
def test_settings_result_carries_the_same_statement(missing_client, language):
    _use_language(missing_client, language)
    _post(missing_client, LATER, next="/")
    settings = missing_client.get("/settings?tab=bambuddy").text
    for phrase in SPEC[language]:
        assert phrase in settings, (language, phrase)


@pytest.mark.parametrize("language", ["en", "de"])
def test_the_guides_tell_what_can_be_changed_later(missing_client, language):
    _use_language(missing_client, language)
    user = missing_client.get("/setup/bambuddy/user").text
    key = missing_client.get("/setup/bambuddy/key").text
    assert ("add a missing right to the group at any time" if language == "en" else "jederzeit ergänzen") in user
    assert ("cannot be changed afterwards" if language == "en" else "nicht nachträglich ändern") in key
