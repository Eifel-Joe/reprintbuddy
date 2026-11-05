"""Setup step 2 in three pages: guide, guide, form with a real permission check (Spec 11.3)."""

import re

import httpx
import pytest
from conftest import REQUIRED, csrf_of, mock_auth_me

from reprintbuddy import settings as st

URL = "http://bambuddy.test:8000"
ME = f"{URL}/api/v1/auth/me"
KEY = "bb_SETUPKEY_123456"
USER_PAGE, KEY_PAGE, CONNECT = "/setup/bambuddy/user", "/setup/bambuddy/key", "/setup/bambuddy/connect"


def _form(token, **over):
    data = {"csrf_token": token, "bambuddy_address": URL, "bambuddy_token": KEY}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _saved(client, key):
    with client.app.state.sessionmaker() as s:
        return st.get_setting(s, key)


def _nothing_saved(client):
    assert _saved(client, "bambuddy_url") is None
    assert _saved(client, "bambuddy_api_key") is None
    assert client.get("/").status_code == 303  # the interface stays closed


def _german(client):
    with client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")


# --- the three pages -------------------------------------------------------------------------


def test_after_admin_goes_to_bambuddy_step(admin_client):
    response = admin_client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/bambuddy"


def test_bambuddy_redirects_to_first_page(admin_client):
    response = admin_client.get("/setup/bambuddy")
    assert response.status_code == 303
    assert response.headers["location"] == USER_PAGE


def test_three_pages_and_progress(admin_client):
    for path, progress in ((USER_PAGE, "Step 2 of 4"), (KEY_PAGE, "Step 3 of 4"), (CONNECT, "Step 4 of 4")):
        page = admin_client.get(path)
        assert page.status_code == 200, path
        assert progress in page.text, path


def test_admin_step_says_step_one_of_four(client):
    assert "Step 1 of 4" in client.get("/setup").text


def test_pages_lead_from_one_to_the_next_and_back(admin_client):
    user, key, connect = (admin_client.get(p).text for p in (USER_PAGE, KEY_PAGE, CONNECT))
    assert f'href="{KEY_PAGE}"' in user
    assert f'href="{USER_PAGE}"' in key and f'href="{CONNECT}"' in key
    assert f'href="{KEY_PAGE}"' in connect


def test_guide_lists_permissions_with_codes(admin_client):
    page = admin_client.get(USER_PAGE).text
    for code in REQUIRED:
        assert f"<code>{code}</code>" in page, code
    assert "Operators" in page and "Administrators" in page  # why no system group fits
    assert "Reprintbuddy" in page
    assert "reprintbuddy" in page
    assert "only once" in page  # the password is needed once, and not asked for


def test_key_guide_names_the_steps(admin_client):
    page = admin_client.get(KEY_PAGE).text
    for needle in ("sign in as", "API keys", "Read Status", "Manage Queue", "only one time", "Sign out"):
        assert needle in page, needle


def test_guide_pages_close_after_setup(ready_client):
    for path in (USER_PAGE, KEY_PAGE, CONNECT):
        closed = ready_client.get(path)
        assert closed.status_code == 303 and closed.headers["location"] == "/settings", path
    response = ready_client.post(CONNECT, data={})
    assert response.status_code == 303 and response.headers["location"] == "/settings"


def test_guide_pages_need_the_setup_of_the_account_first(client):
    response = client.get(USER_PAGE)
    assert response.status_code == 303
    assert response.headers["location"] == "/setup"


def test_not_signed_in_goes_to_login(admin_client):
    admin_client.cookies.clear()
    response = admin_client.get(USER_PAGE)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


# --- the form --------------------------------------------------------------------------------


def test_no_username_field_and_no_checkbox(admin_client):
    page = admin_client.get(CONNECT).text
    assert 'type="checkbox"' not in page
    assert "bambuddy_username" not in page
    names = re.findall(r'<input[^>]*\bname="([^"]+)"', page)
    assert sorted(names) == ["bambuddy_address", "bambuddy_token", "csrf_token"]


def test_autocomplete_attributes(admin_client):
    page = admin_client.get(CONNECT).text
    address = re.search(r'<input[^>]*name="bambuddy_address"[^>]*>', page).group(0)
    token = re.search(r'<input[^>]*name="bambuddy_token"[^>]*>', page).group(0)
    assert 'autocomplete="off"' in address
    assert 'type="password"' in token and 'autocomplete="new-password"' in token
    for field in (address, token):  # the usual hints for password managers
        assert "data-1p-ignore" in field and 'data-lpignore="true"' in field and "data-bwignore" in field
    form = re.search(r"<form[^>]*>", page).group(0)
    assert 'autocomplete="off"' in form
    for forbidden in ('name="username"', 'name="password"', 'name="email"', 'autocomplete="username"'):
        assert forbidden not in page


def test_connect_saves_owner(admin_client, respx_mock):
    mock_auth_me(respx_mock)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert _saved(admin_client, "bambuddy_url") == URL
    assert _saved(admin_client, "bambuddy_username") == "reprintbuddy"  # from Bambuddy's answer
    assert _saved(admin_client, "bambuddy_api_key") == KEY
    assert _saved(admin_client, "bambuddy_review_confirmed") is True
    assert admin_client.get("/").status_code == 200


def test_connect_takes_the_owner_name_from_the_answer(admin_client, respx_mock):
    mock_auth_me(respx_mock, username="print-robot")
    token = csrf_of(admin_client, CONNECT)
    admin_client.post(CONNECT, data=_form(token))
    assert _saved(admin_client, "bambuddy_username") == "print-robot"


def test_connect_saves_in_a_single_commit(admin_client, respx_mock):
    from sqlalchemy import event

    mock_auth_me(respx_mock)
    token = csrf_of(admin_client, CONNECT)
    commits = []
    event.listen(admin_client.app.state.engine, "commit", lambda *a: commits.append(1))
    assert admin_client.post(CONNECT, data=_form(token)).status_code == 303
    assert len(commits) == 1


def _rights(html):
    """{code: 'ok' | 'bad'} from the permission list of a page."""
    found = re.findall(r'<li class="perm (ok|bad)"[^>]*>.*?<code>([^<]+)</code>', html, re.S)
    return {code: state for state, code in found}


def test_connect_missing_permission_lists_it(admin_client, respx_mock):
    permissions = [p for p in REQUIRED if p != "queue:start_unreviewed"]
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 200
    assert "reprintbuddy" in response.text  # the owner is shown
    rights = _rights(response.text)
    assert rights["queue:start_unreviewed"] == "bad"
    assert all(rights[p] == "ok" for p in REQUIRED if p != "queue:start_unreviewed")
    assert "✗" in response.text and "✓" in response.text
    assert f'href="{USER_PAGE}"' in response.text  # where to set it
    assert KEY not in response.text
    _nothing_saved(admin_client)


def test_connect_read_own_is_reported_as_missing(admin_client, respx_mock):
    permissions = ["library:read_own" if p == "library:read_all" else p for p in REQUIRED]
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 200
    assert _rights(response.text)["library:read_all"] == "bad"
    _nothing_saved(admin_client)


def test_connect_admin_owner_warns(admin_client, respx_mock):
    mock_auth_me(respx_mock, username="boss", is_admin=True, groups=("Administrators",))
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 303  # saved nevertheless
    assert _saved(admin_client, "bambuddy_username") == "boss"
    page = admin_client.get(response.headers["location"])
    assert page.status_code == 200
    assert "administrator" in page.text and "more than" in page.text


def test_connect_without_a_warning_for_the_normal_owner(admin_client, respx_mock):
    mock_auth_me(respx_mock)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    page = admin_client.get(response.headers["location"])
    assert "more than" not in page.text


def test_url_trailing_slash_is_normalised(admin_client, respx_mock):
    mock_auth_me(respx_mock)
    token = csrf_of(admin_client, CONNECT)
    admin_client.post(CONNECT, data=_form(token, bambuddy_address=URL + "/"))
    assert _saved(admin_client, "bambuddy_url") == URL


@pytest.mark.parametrize(
    "status,text",
    [(401, "API key was not accepted"), (403, "does not have the rights")],
)
def test_reject_on_refusal(admin_client, respx_mock, status, text):
    mock_auth_me(respx_mock, status=status)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 200
    assert text in response.text
    _nothing_saved(admin_client)


def test_reject_when_unreachable(admin_client, respx_mock):
    respx_mock.get(ME).mock(side_effect=httpx.ConnectError("refused"))
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 200
    assert "Could not reach Bambuddy" in response.text
    _nothing_saved(admin_client)


def test_reject_when_it_is_not_bambuddy(admin_client, respx_mock):
    respx_mock.get(ME).respond(200, html="<html>router</html>")
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 200
    assert "does not look like Bambuddy" in response.text
    _nothing_saved(admin_client)


def test_requires_csrf(admin_client):
    csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(""))
    assert response.status_code == 403
    assert _saved(admin_client, "bambuddy_url") is None


def test_key_not_echoed_on_error(admin_client, respx_mock):
    mock_auth_me(respx_mock, status=401)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert KEY not in response.text
    assert f'value="{URL}"' in response.text  # the address stays in the form


def test_invalid_url_is_reported(admin_client):
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token, bambuddy_address="host:8000"))
    assert response.status_code == 200
    assert "must start with http" in response.text
    assert _saved(admin_client, "bambuddy_url") is None


def test_missing_fields_are_reported(admin_client):
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token, bambuddy_token=""))
    assert response.status_code == 200
    assert "Please fill in" in response.text


def test_key_is_stripped_before_it_is_used_and_saved(admin_client, respx_mock):
    route = mock_auth_me(respx_mock)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token, bambuddy_token="  " + KEY + " \r\n"))
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == KEY
    assert _saved(admin_client, "bambuddy_api_key") == KEY


@pytest.mark.parametrize("bad", ["bb key", "bbüber", "bb\x01key"])
def test_key_with_unusable_characters_gets_its_own_message(admin_client, respx_mock, bad):
    route = mock_auth_me(respx_mock)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token, bambuddy_token=bad))
    assert response.status_code == 200  # no server error, no misleading "unreachable"
    assert "API key contains characters" in response.text
    assert not route.called
    assert _saved(admin_client, "bambuddy_api_key") is None


@pytest.mark.parametrize(
    "bad",
    ["http://h:99999", "http://h:abc", "http://h:0", "http://h:-1", "http://:8000", "http://", "https://[::1", "http://h:8000x"],
)
def test_a_bad_port_is_reported_not_a_server_error(admin_client, bad):
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token, bambuddy_address=bad))
    assert response.status_code == 200
    assert "must start with http" in response.text
    assert _saved(admin_client, "bambuddy_url") is None


def test_german_texts(admin_client, respx_mock):
    _german(admin_client)
    assert "Schritt 2 von 4" in admin_client.get(USER_PAGE).text
    assert "Schritt 3 von 4" in admin_client.get(KEY_PAGE).text
    connect = admin_client.get(CONNECT).text
    assert "Schritt 4 von 4" in connect and "Prüfen und speichern" in connect
    user = admin_client.get(USER_PAGE).text
    for code in REQUIRED:
        assert f"<code>{code}</code>" in user
    assert "Read All Library" in user and "damit Reprintbuddy deine Druckdateien sieht" in user
    assert "Systemgruppe" in user
    permissions = [p for p in REQUIRED if p != "inventory:read"]
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert "Read Inventory" in response.text and "fehlen" in response.text
    for raw_key in ("bambuddy.reason", "bambuddy.result", "permission."):
        assert raw_key not in response.text  # no untranslated keys


EDITOR = {
    "library:read_all": "Read All Library",
    "queue:read_all": "Read All Queue",
    "queue:create": "Create Queue",
    "queue:start_unreviewed": "Print Without Review (else jobs wait for someone to start them)",
    "printers:read": "Read Printers",
    "inventory:read": "Read Inventory",
    "filaments:read": "Read Filaments",
}
KEY_CREATION = {
    "api_keys:read": "Read Api Keys",
    "api_keys:create": "Create Api Keys",
    "settings:read": "Read Settings",
}
PURPOSE_DE = {
    "library:read_all": "damit Reprintbuddy deine Druckdateien sieht",
    "queue:read_all": "damit Reprintbuddy die ganze Warteschlange sieht",
    "queue:create": "damit Reprintbuddy Nachdrucke einreihen kann",
    "queue:start_unreviewed": "damit Nachdrucke ohne Freigabe starten",
    "printers:read": "damit Reprintbuddy die Drucker und ihren Zustand sieht",
    "inventory:read": "damit Reprintbuddy den Filamentbestand liest",
    "filaments:read": "um zu erkennen, ob Bambuddy die Spulen selbst führt oder in Spoolman",
}
PURPOSE_EN = {
    "library:read_all": "so that Reprintbuddy sees your print files",
    "queue:read_all": "so that Reprintbuddy sees the whole queue",
    "queue:create": "so that Reprintbuddy can queue reprints",
    "queue:start_unreviewed": "so that reprints start without approval",
    "printers:read": "so that Reprintbuddy sees the printers and their state",
    "inventory:read": "so that Reprintbuddy reads the filament stock",
    "filaments:read": "to tell whether Bambuddy keeps the spools itself or in Spoolman",
}


def test_editor_labels_are_a_fixed_table_in_the_code():
    from reprintbuddy.bambuddy.connection import EDITOR_LABELS

    assert EDITOR_LABELS == EDITOR


def test_permission_labels_as_in_bambuddy(admin_client, respx_mock):
    """Bambuddy shows its rights in English only: so does Reprintbuddy; the purpose follows the language."""
    permissions = [p for p in REQUIRED if p != "queue:start_unreviewed"]
    mock_auth_me(respx_mock, permissions=permissions)

    def pages():
        token = csrf_of(admin_client, CONNECT)
        result = admin_client.post(CONNECT, data=_form(token))
        return admin_client.get(USER_PAGE).text, result.text

    en_user, en_result = pages()
    _german(admin_client)
    de_user, de_result = pages()
    for page in (en_user, de_user, en_result, de_result):
        for code in REQUIRED:
            assert EDITOR[code] in page and f"<code>{code}</code>" in page, code
        assert "Print Without Review" in page and "Start Unreviewed Queue" not in page
    for page in (en_user, en_result):
        for code in REQUIRED:
            assert PURPOSE_EN[code] in page, code
            assert PURPOSE_DE[code] not in page, code
    for page in (de_user, de_result):
        for code in REQUIRED:
            assert PURPOSE_DE[code] in page, code
            assert PURPOSE_EN[code] not in page, code


KEY_OFF = (
    "Control Printer",
    "Manage Library",
    "Manage Inventory",
    "Manage Maintenance",
    "Manage Archives",
    "Manage Projects",
    "Allow Cloud Access",
    "Update Electricity Price",
    "Send Notifications",
)


def test_key_guide_is_a_table_of_the_settings_with_bambuddys_labels(admin_client):
    page = admin_client.get(KEY_PAGE).text
    table = page[page.index("<table") : page.index("</table>")]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
    on_rows = [r for r in rows if 'class="on"' in r]
    off_rows = [r for r in rows if 'class="off"' in r]
    assert len(on_rows) == 2 and "Read Status" in on_rows[0] and "Manage Queue" in on_rows[1]
    assert len(off_rows) == len(KEY_OFF)
    for label in KEY_OFF:
        assert any(label in r for r in off_rows), label
    assert "Reprintbuddy" in table
    assert len(rows) == 1 + 2 + len(KEY_OFF)  # name, two on, the rest off: nothing else


def test_key_guide_has_no_printer_or_expiry_row(admin_client):
    """The Bambuddy dialog has no such fields."""
    for language in ("en", "de"):
        with admin_client.app.state.sessionmaker() as s:
            st.set_setting(s, "language", language)
        page = admin_client.get(KEY_PAGE).text
        for word in ("expiry", "Expiry", "Ablaufdatum", "no restriction", "keine Einschränkung"):
            assert word not in page, (language, word)
        assert "<th scope=\"row\">Printers" not in page and "<th scope=\"row\">Drucker<" not in page


def test_key_guide_in_german_has_bambuddys_labels_and_german_purposes(admin_client):
    _german(admin_client)
    page = admin_client.get(KEY_PAGE).text
    assert "Read Status" in page and "Manage Queue" in page
    assert "Status lesen" not in page and "Warteschlange verwalten" not in page  # no translated right names
    assert "damit Reprintbuddy" in page
    assert "Control Printer" in page and "Drucker steuern" not in page


def _item(html, code):
    """The list item of one right."""
    pattern = r'<li class="perm[^>]*>(?:(?!</li>).)*<code>' + re.escape(code) + r"</code>.*?</li>"
    return re.search(pattern, html, re.S).group(0)


def test_missing_permission_names_group_and_key(admin_client, respx_mock):
    permissions = [p for p in REQUIRED if p not in ("queue:start_unreviewed", "library:read_all")]
    mock_auth_me(respx_mock, permissions=permissions)
    token = csrf_of(admin_client, CONNECT)
    page = admin_client.post(CONNECT, data=_form(token)).text
    queue_item = _item(page, "queue:start_unreviewed")
    assert "Manage Queue" in queue_item and "Read Status" not in queue_item
    assert "Reprintbuddy" in queue_item
    assert f'href="{USER_PAGE}"' in queue_item and f'href="{KEY_PAGE}"' in queue_item
    read_item = _item(page, "library:read_all")
    assert "Read Status" in read_item and "Manage Queue" not in read_item
    assert f'href="{USER_PAGE}"' in read_item and f'href="{KEY_PAGE}"' in read_item
    assert "where it can be missing" in queue_item.lower()


def test_which_key_right_covers_which_permission(admin_client, respx_mock):
    """Reading rights come with 'Read Status', queue rights with 'Manage Queue'."""
    mock_auth_me(respx_mock, permissions=[])
    token = csrf_of(admin_client, CONNECT)
    page = admin_client.post(CONNECT, data=_form(token)).text
    for code in ("library:read_all", "queue:read_all", "printers:read", "inventory:read"):
        assert "Read Status" in _item(page, code), code
    for code in ("queue:create", "queue:start_unreviewed"):
        assert "Manage Queue" in _item(page, code), code
    _german(admin_client)
    token = csrf_of(admin_client, CONNECT)
    page = admin_client.post(CONNECT, data=_form(token)).text
    assert "Read Status" in _item(page, "printers:read")
    assert "Manage Queue" in _item(page, "queue:create")
    assert "Wo es fehlen kann" in _item(page, "queue:create")


def test_guide_names_the_rights_for_creating_the_key(admin_client):
    page = admin_client.get(USER_PAGE).text
    for code, label in KEY_CREATION.items():
        assert f"<code>{code}</code>" in page, code
        assert label in page, code
    assert page.lower().count("only to create the key") >= 4  # the sentence and one note per right
    assert "may be removed again" in page and "does not need them" in page


def test_guide_names_the_rights_for_creating_the_key_in_german(admin_client):
    _german(admin_client)
    page = admin_client.get(USER_PAGE).text
    for code, label in KEY_CREATION.items():
        assert f"<code>{code}</code>" in page and label in page, code
    assert page.lower().count("nur zum erzeugen des schlüssels") >= 4
    assert "Nach dem Erzeugen des Schlüssels dürfen diese drei Rechte wieder weg; der Schlüssel braucht sie nicht." in page


def test_the_key_creation_rights_are_not_checked(admin_client, respx_mock):
    """A key without them passes: Reprintbuddy needs them only while the key is created."""
    mock_auth_me(respx_mock)  # exactly the six required rights, no api_keys:*, no settings:read
    token = csrf_of(admin_client, CONNECT)
    response = admin_client.post(CONNECT, data=_form(token))
    assert response.status_code == 303
    assert _saved(admin_client, "bambuddy_api_key") == KEY


def test_the_key_creation_rights_are_not_in_the_result_list(admin_client, respx_mock):
    mock_auth_me(respx_mock, permissions=[])
    token = csrf_of(admin_client, CONNECT)
    page = admin_client.post(CONNECT, data=_form(token)).text
    assert "api_keys" not in page and "settings:read" not in page
