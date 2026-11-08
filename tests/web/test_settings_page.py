import html
import re

import httpx
import pytest
from conftest import ADMIN, api_routes, auth_me, csrf_of, PASSWORD

from reprintbuddy import settings as st

OLD_URL = "http://bambuddy.test:8000"
NEW_URL = "http://other.test:8000"
CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
KEY_PEM = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"


def saved(client, key):
    with client.app.state.sessionmaker() as s:
        return st.get_setting(s, key)


def post(client, path, **data):
    token = csrf_of(client, "/settings")
    return client.post(path, data={"csrf_token": token, **data})


def bambuddy_form(**over):
    data = {
        "bambuddy_address": OLD_URL,
        "bambuddy_token": "",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def test_page_renders_all_sections(ready_client):
    texts = ""
    for tab in ("general", "bambuddy", "account"):
        response = ready_client.get(f"/settings?tab={tab}")
        assert response.status_code == 200
        texts += response.text
    for action in (
        "/settings/language",
        "/settings/bambuddy",
        "/settings/password",
        "/settings/server",
    ):
        assert f'action="{action}"' in texts
    assert OLD_URL in ready_client.get("/settings?tab=bambuddy").text


def test_language_change(ready_client):
    response = post(ready_client, "/settings/language", language="de")
    assert response.status_code == 303
    page = ready_client.get("/settings")
    assert "Einstellungen" in page.text
    assert saved(ready_client, "language") == "de"


def test_language_invalid_is_rejected(ready_client):
    response = post(ready_client, "/settings/language", language="fr")
    assert response.status_code == 200
    assert saved(ready_client, "language") == "en"


def test_bambuddy_change_checked(ready_client, respx_mock):
    respx_mock.get(f"{NEW_URL}/api/v1/auth/me").respond(401)
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_address=NEW_URL, bambuddy_token="bb_wrong"),
    )
    assert response.status_code == 200
    assert "API key was not accepted" in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


def test_bambuddy_empty_key_keeps_old(ready_client, respx_mock):
    # same address, empty key field: the stored key is reused for the check and kept;
    # the owner comes from Bambuddy's answer
    route = respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me(username="other-user"))
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form())
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_testkey"
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"
    assert saved(ready_client, "bambuddy_username") == "other-user"


def test_bambuddy_same_address_written_differently_keeps_key(ready_client, respx_mock):
    route = respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=OLD_URL + "/")
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_testkey"


@pytest.mark.parametrize(
    "new_url",
    [NEW_URL, "https://bambuddy.test:8000", "http://bambuddy.test:9000"],
    ids=["host", "scheme", "port"],
)
def test_settings_changed_address_needs_key(ready_client, respx_mock, new_url):
    """A stored key is never sent to a different address."""
    route = respx_mock.get(f"{new_url}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=new_url)
    )
    assert response.status_code == 200
    assert "enter the API key again" in response.text
    assert not route.called, "the stored key must not leave for the new address"
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


def test_bambuddy_new_address_with_new_key_is_saved(ready_client, respx_mock):
    route = respx_mock.get(f"{NEW_URL}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_address=NEW_URL, bambuddy_token="bb_for_new"),
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_for_new"
    assert saved(ready_client, "bambuddy_url") == NEW_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_for_new"


def test_bambuddy_new_key_overwrites(ready_client, respx_mock):
    route = respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_token="bb_new")
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_new"
    assert saved(ready_client, "bambuddy_api_key") == "bb_new"


def test_bambuddy_unreachable_changes_nothing(ready_client, respx_mock):
    respx_mock.get(f"{NEW_URL}/api/v1/auth/me").mock(side_effect=httpx.ConnectError("x"))
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_address=NEW_URL, bambuddy_token="bb_for_new"),
    )
    assert response.status_code == 200
    assert "Could not reach Bambuddy" in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert NEW_URL in response.text  # the typed address stays in the form


def test_settings_shows_owner(ready_client):
    page = ready_client.get("/settings?tab=bambuddy").text
    assert "Saved key owner: reprintbuddy" in page


def test_settings_no_username_field_and_no_checkbox(ready_client):
    import re

    page = ready_client.get("/settings?tab=bambuddy").text
    section = page[page.index('action="/settings/bambuddy"') : page.index("</form>", page.index('action="/settings/bambuddy"'))]
    assert "bambuddy_username" not in section and 'type="checkbox"' not in section
    # The address is a text area since v0.3.8 (p113); the key and the token are fields.
    assert sorted(re.findall(r'<(?:input|textarea)[^>]*\bname="([^"]+)"', section)) == ["bambuddy_address", "bambuddy_token", "csrf_token"]


def test_settings_autocomplete_attributes(ready_client):
    import re

    page = ready_client.get("/settings?tab=bambuddy").text
    address = re.search(r'<textarea[^>]*name="bambuddy_address"[^>]*>', page).group(0)
    token = re.search(r'<input[^>]*name="bambuddy_token"[^>]*>', page).group(0)
    assert 'autocomplete="off"' in address
    assert 'type="password"' in token and 'autocomplete="new-password"' in token
    for field in (address, token):
        assert "data-1p-ignore" in field and 'data-lpignore="true"' in field and "data-bwignore" in field


def test_settings_check_missing_permission(ready_client, respx_mock):
    from conftest import REQUIRED

    permissions = [p for p in REQUIRED if p != "queue:start_unreviewed"]
    respx_mock.get(f"{NEW_URL}/api/v1/auth/me").respond(200, json=auth_me(username="someone", permissions=permissions))
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=NEW_URL, bambuddy_token="bb_for_new")
    )
    assert response.status_code == 200
    assert "Owner of the key: someone" in response.text
    import re

    rights = dict((code, state) for state, code in re.findall(r'<li class="perm (ok|bad)"[^>]*>.*?<code>([^<]+)</code>', response.text, re.S))
    assert rights["queue:start_unreviewed"] == "bad"
    assert all(rights[p] == "ok" for p in REQUIRED if p != "queue:start_unreviewed")
    assert "bb_for_new" not in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"
    assert saved(ready_client, "bambuddy_username") == "reprintbuddy"


def _tag_with(page, text):
    """The opening tag of the <p> that holds `text`."""
    end = page.index(text)
    return page[page.rindex("<p", 0, end) : end]


def _is_warning(tag):
    return 'class="alert"' in tag and 'role="alert"' in tag


def _is_plain_notice(tag):
    return 'class="notice"' in tag and 'role="status"' in tag


def test_a_plain_save_stays_in_the_calm_look(ready_client, respx_mock):
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form())
    assert response.status_code == 303
    assert _is_plain_notice(_tag_with(ready_client.get("/settings").text, "Saved."))


def test_settings_check_admin_owner_warns(ready_client, respx_mock):
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me(username="boss", is_admin=True))
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form())
    assert response.status_code == 303
    assert saved(ready_client, "bambuddy_username") == "boss"
    page = ready_client.get("/settings")
    assert "administrator" in page.text and "more than" in page.text
    assert _is_warning(_tag_with(page.text, "The key belongs to an administrator"))  # a warning, not a success
    assert "administrator" not in ready_client.get("/settings").text  # shown once


def test_settings_without_a_stored_key_and_no_entry_is_reported(ready_client):
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=""))
    assert response.status_code == 200
    assert "Please fill in" in response.text


def test_password_change(ready_client):
    wrong = post(
        ready_client,
        "/settings/password",
        current_password="not-my-password",
        new_password="brand-new-pass1",
        new_password_repeat="brand-new-pass1",
    )
    assert wrong.status_code == 200
    assert "current password is wrong" in wrong.text

    ok = post(
        ready_client,
        "/settings/password",
        current_password=PASSWORD,
        new_password="brand-new-pass1",
        new_password_repeat="brand-new-pass1",
    )
    assert ok.status_code == 303

    ready_client.cookies.clear()
    token = csrf_of(ready_client, "/login")
    old = ready_client.post(
        "/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD}
    )
    assert old.status_code == 200  # old password no longer works
    token = csrf_of(ready_client, "/login")
    new = ready_client.post(
        "/login",
        data={"csrf_token": token, "username": ADMIN, "password": "brand-new-pass1"},
    )
    assert new.status_code == 303


@pytest.mark.parametrize(
    "new,repeat,message",
    [
        ("brand-new-pass1", "different-pass1", "do not match"),
        ("short", "short", "at least 8 characters"),
    ],
)
def test_password_change_rejects_bad_input(ready_client, new, repeat, message):
    response = post(
        ready_client,
        "/settings/password",
        current_password=PASSWORD,
        new_password=new,
        new_password_repeat=repeat,
    )
    assert response.status_code == 200
    assert message in response.text


def test_server_port_invalid(ready_client):
    for bad in ("70000", "0", "abc"):
        response = post(ready_client, "/settings/server", host="0.0.0.0", port=bad)
        assert response.status_code == 200, bad
        assert "port must be a number" in response.text
    assert saved(ready_client, "port") == 8080


def test_server_saves_and_hints_restart(ready_client):
    response = post(ready_client, "/settings/server", host="127.0.0.1", port="9090")
    assert response.status_code == 303
    assert saved(ready_client, "host") == "127.0.0.1"
    assert saved(ready_client, "port") == 9090
    assert "restart" in ready_client.get("/settings").text.lower()


def test_server_tls_text_fields(ready_client, tls_pair):
    cert, key = tls_pair
    response = post(
        ready_client,
        "/settings/server",
        host="0.0.0.0",
        port="8443",
        tls_cert=cert,
        tls_key=key,
    )
    assert response.status_code == 303
    assert saved(ready_client, "tls_cert") == cert
    assert saved(ready_client, "tls_key") == key


def test_server_tls_file_upload(ready_client, tls_pair):
    cert, key = tls_pair
    token = csrf_of(ready_client, "/settings")
    response = ready_client.post(
        "/settings/server",
        data={"csrf_token": token, "host": "0.0.0.0", "port": "8443"},
        files={
            "tls_cert_file": ("cert.pem", cert.encode(), "application/x-pem-file"),
            "tls_key_file": ("key.pem", key.encode(), "application/x-pem-file"),
        },
    )
    assert response.status_code == 303
    assert saved(ready_client, "tls_cert") == cert
    assert saved(ready_client, "tls_key") == key


def test_server_tls_needs_both_parts(ready_client, tls_pair):
    response = post(
        ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=tls_pair[0]
    )
    assert response.status_code == 200
    assert "needs both" in response.text
    assert saved(ready_client, "tls_cert") is None
    assert saved(ready_client, "port") == 8080


def test_server_empty_tls_fields_keep_stored_values(ready_client, tls_pair):
    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    post(ready_client, "/settings/server", host="0.0.0.0", port="8444")
    assert saved(ready_client, "tls_key") == key
    assert saved(ready_client, "tls_cert") == cert
    assert saved(ready_client, "port") == 8444


def test_server_tls_can_be_removed(ready_client, tls_pair):
    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    post(ready_client, "/settings/server", host="0.0.0.0", port="8080", tls_remove="on")
    assert saved(ready_client, "tls_cert") is None
    assert saved(ready_client, "tls_key") is None


def test_server_rejects_mismatched_tls(ready_client):
    from conftest import make_tls_pair

    cert_a, _ = make_tls_pair("a.test")
    _, key_b = make_tls_pair("b.test")
    response = post(
        ready_client, "/settings/server", host="0.0.0.0", port="9090", tls_cert=cert_a, tls_key=key_b
    )
    assert response.status_code == 200
    assert "do not match" in response.text
    assert saved(ready_client, "tls_cert") is None
    assert saved(ready_client, "tls_key") is None
    assert saved(ready_client, "port") == 8080  # nothing at all is saved


def test_server_rejects_mismatch_against_stored_half(ready_client, tls_pair):
    """A new certificate with the stored key is checked as a pair, too."""
    from conftest import make_tls_pair

    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    other_cert, _ = make_tls_pair("other.test")
    response = post(ready_client, "/settings/server", host="0.0.0.0", port="9999", tls_cert=other_cert)
    assert response.status_code == 200
    assert "do not match" in response.text
    assert saved(ready_client, "tls_cert") == cert
    assert saved(ready_client, "port") == 8443


def test_server_rejects_encrypted_key(ready_client, tls_pair):
    cert, _ = tls_pair
    encrypted = "-----BEGIN ENCRYPTED PRIVATE KEY-----\nAAAA\n-----END ENCRYPTED PRIVATE KEY-----\n"
    response = post(
        ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=encrypted
    )
    assert response.status_code == 200
    assert "Password-protected private keys are not supported" in response.text
    assert saved(ready_client, "tls_cert") is None


def test_server_saves_valid_tls_and_masks_key(ready_client, tls_pair):
    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    page = ready_client.get("/settings").text
    assert "********" in page
    assert "PRIVATE KEY" not in page


def test_server_tls_remove_requires_csrf(ready_client, tls_pair):
    cert, key = tls_pair
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=cert, tls_key=key)
    response = ready_client.post(
        "/settings/server", data={"host": "0.0.0.0", "port": "8080", "tls_remove": "on"}
    )
    assert response.status_code == 403
    assert saved(ready_client, "tls_cert") == cert


def test_server_tls_rejects_non_pem(ready_client):
    response = post(
        ready_client,
        "/settings/server",
        host="0.0.0.0",
        port="8443",
        tls_cert="not a certificate",
        tls_key=KEY_PEM,
    )
    assert response.status_code == 200
    assert saved(ready_client, "tls_cert") is None


def test_all_settings_posts_require_csrf(ready_client):
    paths = sorted(
        route.path
        for route in api_routes(ready_client.app)
        if "POST" in route.methods
        and route.path.startswith("/settings")
    )
    assert paths == [
        "/settings/automation",
        "/settings/backup",
        "/settings/bambuddy",
        "/settings/language",
        "/settings/password",
        "/settings/qr",
        "/settings/restart",
        "/settings/restore",
        "/settings/restore/cancel",
        "/settings/restore/upload",
        "/settings/server",
        "/settings/update",
        "/settings/update/auto",
        "/settings/update/check",
    ]
    csrf_of(ready_client, "/settings")  # a session with a token exists
    for path in paths:
        assert ready_client.post(path, data={}).status_code == 403, path
        assert ready_client.post(path, data={"csrf_token": "forged"}).status_code == 403, path
    assert saved(ready_client, "language") == "en"


@pytest.mark.parametrize("host", ["203.0.113.77", "no-such-host.invalid", "256.1.1.1"])
def test_server_host_that_cannot_be_bound_is_refused(ready_client, host):
    """Saving such an address would lock everybody out after the next restart."""
    response = post(ready_client, "/settings/server", host=host, port="8080")
    assert response.status_code == 200
    assert "cannot listen on this address" in response.text
    assert saved(ready_client, "host") == "0.0.0.0"


def test_settings_key_is_stripped(ready_client, respx_mock):
    route = respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_token=" bb_trimmed \n"))
    assert route.calls.last.request.headers["x-api-key"] == "bb_trimmed"
    assert saved(ready_client, "bambuddy_api_key") == "bb_trimmed"


@pytest.mark.parametrize("bad", ["bb key", "bbüber", "bb\x01key"])
def test_settings_key_with_unusable_characters(ready_client, respx_mock, bad):
    route = respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_token=bad))
    assert response.status_code == 200
    assert "API key contains characters" in response.text
    assert not route.called
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


@pytest.mark.parametrize("bad", ["http://h:99999", "http://h:abc", "http://h:0", "http://h:-1", "http://:8000", "http://", "https://[::1", "http://h:8000x"])
def test_settings_a_bad_port_is_reported_not_a_server_error(ready_client, bad):
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=bad, bambuddy_token="bb_x"))
    assert response.status_code == 200
    assert "must start with http" in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL


def test_settings_a_bad_port_without_a_key_is_reported_too(ready_client):
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address="http://bambuddy.test:99999"))
    assert response.status_code == 200
    assert saved(ready_client, "bambuddy_url") == OLD_URL


def test_a_port_below_1024_is_saved_with_a_hint(ready_client):
    response = post(ready_client, "/settings/server", host="127.0.0.1", port="80")
    assert response.status_code == 303
    assert saved(ready_client, "port") == 80
    page = ready_client.get("/settings").text
    assert "below 1024" in page and "set-port" in page
    assert "install script" in page  # the installed service has the right to use such a port
    assert "does not run as root" not in page  # it does bind low ports: CAP_NET_BIND_SERVICE
    assert _is_warning(_tag_with(page, "Saved. Note: ports below 1024"))


def test_a_normal_port_has_no_privileged_hint(ready_client):
    post(ready_client, "/settings/server", host="127.0.0.1", port="8081")
    page = ready_client.get("/settings").text
    assert "below 1024" not in page
    assert "restarted" in page  # the usual restart hint is still there
    assert _is_plain_notice(_tag_with(page, "Changes to address, port and HTTPS"))


def test_settings_list_has_bambuddys_label_purpose_and_where_to_fix(ready_client, respx_mock):
    from conftest import REQUIRED

    body = auth_me(permissions=[p for p in REQUIRED if p != "queue:create"])
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=body)
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form())
    assert response.status_code == 200
    assert "Create Queue" in response.text and "so that Reprintbuddy can queue reprints" in response.text
    assert "Manage Queue" in response.text
    assert 'href="/setup/bambuddy/user"' in response.text and 'href="/setup/bambuddy/key"' in response.text
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form())
    assert "Create Queue" in response.text and "damit Reprintbuddy Nachdrucke einreihen kann" in response.text
    assert "Manage Queue" in response.text


# p106 (v0.3.7): "… mit dem Reprintbuddy arbeitet Ein Wert ist gespeichert." – the field's hint and
# the sentence "a value is stored" ran together without a full stop.
STORED_SENTENCE = {"en": "A value is stored.", "de": "Ein Wert ist gespeichert."}


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("tab,field", [("bambuddy", "bambuddy_token"), ("general", "tls_key")])
def test_hint_and_stored_sentence_are_separate_sentences(ready_client, tls_pair, language, tab, field):
    cert, key = tls_pair
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", language)
        st.set_setting(s, "tls_cert", cert)
        st.set_setting(s, "tls_key", key)
    page = ready_client.get(f"/settings?tab={tab}").text
    hint = re.search(rf'<label for="{field}">.*?<span>(.*?)</span>', page, re.S)
    assert hint, "the field has a hint"
    text = html.unescape(hint.group(1))
    sentence = STORED_SENTENCE[language]
    assert sentence in text
    before = text.split(sentence)[0].rstrip()
    assert before.endswith("."), f"no full stop before the stored sentence: {text!r}"
    assert "  " not in text


@pytest.mark.parametrize("language", ["en", "de"])
def test_hints_of_secret_fields_end_with_a_full_stop_on_their_own(language):
    import json
    from importlib.resources import files

    texts = json.loads(files("reprintbuddy.locales").joinpath(f"{language}.json").read_text(encoding="utf-8"))
    for key in ("settings.bambuddy_key_hint", "settings.tls_key_hint"):
        assert texts[key].endswith("."), key


def test_a_hint_that_already_ends_in_a_full_stop_gets_no_second_one(ready_client):
    env = ready_client.app.state.templates.env
    template = env.from_string('{% import "_ui.html" as ui %}{{ ui.with_stored(hint, "Stored.") }}')
    assert template.render(hint="One.").strip() == "One. Stored."
    assert template.render(hint="One").strip() == "One. Stored."
    assert template.render(hint="Really?").strip() == "Really? Stored."
    assert template.render(hint="One", ).strip() == "One. Stored."


# p97 (v0.3.7): "check and save" wakes the live channel; a failed check wakes nothing.


class WakeRecorder:
    """Stands in for the live listener of the app: records every wake."""

    def __init__(self):
        self.wakes: list[bool] = []
        self.state = "no_right"

    def wake(self, *, reconnect: bool = False) -> None:
        self.wakes.append(reconnect)


def test_a_saved_connection_wakes_the_live_channel(ready_client, respx_mock):
    recorder = ready_client.app.state.live_listener = WakeRecorder()
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form())
    assert response.status_code == 303
    assert recorder.wakes == [False]  # same address, same key: a standing connection is left alone


def test_a_new_key_or_address_replaces_a_standing_connection(ready_client, respx_mock):
    recorder = ready_client.app.state.live_listener = WakeRecorder()
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_token="bb_new"))
    respx_mock.get(f"{NEW_URL}/api/v1/auth/me").respond(200, json=auth_me())
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=NEW_URL, bambuddy_token="bb_other"))
    assert recorder.wakes == [True, True]


def test_the_same_key_typed_again_counts_as_unchanged(ready_client, respx_mock):
    recorder = ready_client.app.state.live_listener = WakeRecorder()
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").respond(200, json=auth_me())
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_token="bb_testkey"))
    assert recorder.wakes == [False]


def test_a_failed_check_wakes_nothing(ready_client, respx_mock):
    recorder = ready_client.app.state.live_listener = WakeRecorder()
    respx_mock.get(f"{NEW_URL}/api/v1/auth/me").respond(401)
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=NEW_URL, bambuddy_token="bb_x"))
    assert response.status_code == 200
    respx_mock.get(f"{OLD_URL}/api/v1/auth/me").mock(side_effect=httpx.ConnectError("x"))
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_token="bb_y"))
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address="", bambuddy_token="bb_z"))
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address="not an address"))
    assert recorder.wakes == []


def test_a_changed_path_prefix_replaces_a_standing_connection(ready_client, respx_mock):
    # Review of v0.3.7: scheme, host and port are the same, the path prefix of a reverse proxy is not.
    recorder = ready_client.app.state.live_listener = WakeRecorder()
    respx_mock.get(f"{OLD_URL}/bb/api/v1/auth/me").respond(200, json=auth_me())
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=f"{OLD_URL}/bb", bambuddy_token="bb_testkey"))
    assert recorder.wakes == [True]
    respx_mock.get(f"{OLD_URL}/bb/api/v1/auth/me").respond(200, json=auth_me())
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_address=f"{OLD_URL}/bb/"))  # only a trailing slash
    assert recorder.wakes == [True, False]
