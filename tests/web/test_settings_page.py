import httpx
import pytest
from conftest import ADMIN, PASSWORD, api_routes, csrf_of

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
        "bambuddy_url": OLD_URL,
        "bambuddy_username": "reprintbuddy",
        "bambuddy_api_key": "",
        "bambuddy_review_confirmed": "on",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def test_page_renders_all_sections(ready_client):
    response = ready_client.get("/settings")
    assert response.status_code == 200
    for action in (
        "/settings/language",
        "/settings/bambuddy",
        "/settings/password",
        "/settings/server",
    ):
        assert f'action="{action}"' in response.text
    assert OLD_URL in response.text


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
    respx_mock.get(f"{NEW_URL}/api/v1/queue/").respond(401)
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_url=NEW_URL, bambuddy_api_key="bb_wrong"),
    )
    assert response.status_code == 200
    assert "API key was not accepted" in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


def test_bambuddy_empty_key_keeps_old(ready_client, respx_mock):
    # same address, another user name: the stored key is reused for the check and kept
    route = respx_mock.get(f"{OLD_URL}/api/v1/queue/").respond(200, json=[])
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_username="other-user")
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_testkey"
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"
    assert saved(ready_client, "bambuddy_username") == "other-user"


def test_bambuddy_same_address_written_differently_keeps_key(ready_client, respx_mock):
    route = respx_mock.get(f"{OLD_URL}/api/v1/queue/").respond(200, json=[])
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_url=OLD_URL + "/")
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_testkey"


@pytest.mark.parametrize(
    "new_url",
    [NEW_URL, "https://bambuddy.test:8000", "http://bambuddy.test:9000"],
    ids=["host", "scheme", "port"],
)
def test_bambuddy_new_address_needs_the_key_again(ready_client, respx_mock, new_url):
    """A stored key is never sent to a different address."""
    route = respx_mock.get(f"{new_url}/api/v1/queue/").respond(200, json=[])
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_url=new_url)
    )
    assert response.status_code == 200
    assert "enter the API key again" in response.text
    assert not route.called, "the stored key must not leave for the new address"
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


def test_bambuddy_new_address_with_new_key_is_saved(ready_client, respx_mock):
    route = respx_mock.get(f"{NEW_URL}/api/v1/queue/").respond(200, json=[])
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_url=NEW_URL, bambuddy_api_key="bb_for_new"),
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_for_new"
    assert saved(ready_client, "bambuddy_url") == NEW_URL
    assert saved(ready_client, "bambuddy_api_key") == "bb_for_new"


def test_bambuddy_new_key_overwrites(ready_client, respx_mock):
    route = respx_mock.get(f"{OLD_URL}/api/v1/queue/").respond(200, json=[])
    response = post(
        ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_api_key="bb_new")
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == "bb_new"
    assert saved(ready_client, "bambuddy_api_key") == "bb_new"


def test_bambuddy_unreachable_changes_nothing(ready_client, respx_mock):
    respx_mock.get(f"{NEW_URL}/api/v1/queue/").mock(side_effect=httpx.ConnectError("x"))
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_url=NEW_URL, bambuddy_api_key="bb_for_new"),
    )
    assert response.status_code == 200
    assert "Could not reach Bambuddy" in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL
    assert NEW_URL in response.text  # the typed address stays in the form


def test_bambuddy_review_checkbox_required(ready_client):
    response = post(
        ready_client,
        "/settings/bambuddy",
        **bambuddy_form(bambuddy_review_confirmed=None),
    )
    assert response.status_code == 200
    assert "Print without review" in response.text


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


def test_server_tls_text_fields(ready_client):
    response = post(
        ready_client,
        "/settings/server",
        host="0.0.0.0",
        port="8443",
        tls_cert=CERT,
        tls_key=KEY_PEM,
    )
    assert response.status_code == 303
    assert saved(ready_client, "tls_cert") == CERT
    assert saved(ready_client, "tls_key") == KEY_PEM


def test_server_tls_file_upload(ready_client):
    token = csrf_of(ready_client, "/settings")
    response = ready_client.post(
        "/settings/server",
        data={"csrf_token": token, "host": "0.0.0.0", "port": "8443"},
        files={
            "tls_cert_file": ("cert.pem", CERT.encode(), "application/x-pem-file"),
            "tls_key_file": ("key.pem", KEY_PEM.encode(), "application/x-pem-file"),
        },
    )
    assert response.status_code == 303
    assert saved(ready_client, "tls_cert") == CERT
    assert saved(ready_client, "tls_key") == KEY_PEM


def test_server_tls_needs_both_parts(ready_client):
    response = post(
        ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=CERT
    )
    assert response.status_code == 200
    assert "certificate and the private key" in response.text
    assert saved(ready_client, "tls_cert") is None


def test_server_empty_tls_fields_keep_stored_values(ready_client):
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=CERT, tls_key=KEY_PEM)
    post(ready_client, "/settings/server", host="0.0.0.0", port="8444")
    assert saved(ready_client, "tls_key") == KEY_PEM
    assert saved(ready_client, "tls_cert") == CERT
    assert saved(ready_client, "port") == 8444


def test_server_tls_can_be_removed(ready_client):
    post(ready_client, "/settings/server", host="0.0.0.0", port="8443", tls_cert=CERT, tls_key=KEY_PEM)
    post(ready_client, "/settings/server", host="0.0.0.0", port="8080", tls_remove="on")
    assert saved(ready_client, "tls_cert") is None
    assert saved(ready_client, "tls_key") is None


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
        "/settings/bambuddy",
        "/settings/language",
        "/settings/password",
        "/settings/restart",
        "/settings/server",
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
    route = respx_mock.get(f"{OLD_URL}/api/v1/queue/").respond(200, json=[])
    post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_api_key=" bb_trimmed \n"))
    assert route.calls.last.request.headers["x-api-key"] == "bb_trimmed"
    assert saved(ready_client, "bambuddy_api_key") == "bb_trimmed"


@pytest.mark.parametrize("bad", ["bb key", "bbüber", "bb\x01key"])
def test_settings_key_with_unusable_characters(ready_client, respx_mock, bad):
    route = respx_mock.get(f"{OLD_URL}/api/v1/queue/").respond(200, json=[])
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_api_key=bad))
    assert response.status_code == 200
    assert "API key contains characters" in response.text
    assert not route.called
    assert saved(ready_client, "bambuddy_api_key") == "bb_testkey"


@pytest.mark.parametrize("bad", ["http://h:99999", "http://h:abc", "http://h:0", "http://h:-1", "http://:8000", "http://", "https://[::1", "http://h:8000x"])
def test_settings_a_bad_port_is_reported_not_a_server_error(ready_client, bad):
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_url=bad, bambuddy_api_key="bb_x"))
    assert response.status_code == 200
    assert "must start with http" in response.text
    assert saved(ready_client, "bambuddy_url") == OLD_URL


def test_settings_a_bad_port_without_a_key_is_reported_too(ready_client):
    response = post(ready_client, "/settings/bambuddy", **bambuddy_form(bambuddy_url="http://bambuddy.test:99999"))
    assert response.status_code == 200
    assert saved(ready_client, "bambuddy_url") == OLD_URL


def test_a_port_below_1024_is_saved_with_a_hint(ready_client):
    response = post(ready_client, "/settings/server", host="127.0.0.1", port="80")
    assert response.status_code == 303
    assert saved(ready_client, "port") == 80
    page = ready_client.get("/settings").text
    assert "below 1024" in page and "root" in page and "set-port" in page


def test_a_normal_port_has_no_privileged_hint(ready_client):
    post(ready_client, "/settings/server", host="127.0.0.1", port="8081")
    page = ready_client.get("/settings").text
    assert "below 1024" not in page
    assert "restarted" in page  # the usual restart hint is still there
