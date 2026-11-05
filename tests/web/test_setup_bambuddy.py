import httpx
import pytest
from conftest import csrf_of

from reprintbuddy import settings as st

URL = "http://bambuddy.test:8000"
QUEUE = f"{URL}/api/v1/queue/"
KEY = "bb_SETUPKEY_123456"


def _form(token, **over):
    data = {
        "csrf_token": token,
        "bambuddy_url": URL,
        "bambuddy_username": "reprintbuddy",
        "bambuddy_api_key": KEY,
        "bambuddy_review_confirmed": "on",
    }
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def _saved(client, key):
    with client.app.state.sessionmaker() as s:
        return st.get_setting(s, key)


def test_after_admin_goes_to_bambuddy_step(admin_client):
    response = admin_client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/setup/bambuddy"
    page = admin_client.get("/setup/bambuddy")
    assert page.status_code == 200
    assert 'type="password"' in page.text
    assert 'autocomplete="off"' in page.text


def test_save_on_success(admin_client, respx_mock):
    respx_mock.get(QUEUE).respond(200, json=[])
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token))
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert _saved(admin_client, "bambuddy_url") == URL
    assert _saved(admin_client, "bambuddy_username") == "reprintbuddy"
    assert _saved(admin_client, "bambuddy_api_key") == KEY
    assert _saved(admin_client, "bambuddy_review_confirmed") is True
    assert admin_client.get("/").status_code == 200


def test_url_trailing_slash_is_normalised(admin_client, respx_mock):
    respx_mock.get(QUEUE).respond(200, json=[])
    token = csrf_of(admin_client, "/setup/bambuddy")
    admin_client.post("/setup/bambuddy", data=_form(token, bambuddy_url=URL + "/"))
    assert _saved(admin_client, "bambuddy_url") == URL


@pytest.mark.parametrize(
    "status,text",
    [(401, "API key was not accepted"), (403, "does not have the rights")],
)
def test_reject_on_refusal(admin_client, respx_mock, status, text):
    respx_mock.get(QUEUE).respond(status)
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token))
    assert response.status_code == 200
    assert text in response.text
    assert _saved(admin_client, "bambuddy_api_key") is None
    assert _saved(admin_client, "bambuddy_url") is None
    assert admin_client.get("/").status_code == 303  # still locked


def test_reject_when_unreachable(admin_client, respx_mock):
    respx_mock.get(QUEUE).mock(side_effect=httpx.ConnectError("refused"))
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token))
    assert response.status_code == 200
    assert "Could not reach Bambuddy" in response.text
    assert _saved(admin_client, "bambuddy_url") is None


def test_review_checkbox_required(admin_client):
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post(
        "/setup/bambuddy", data=_form(token, bambuddy_review_confirmed=None)
    )
    assert response.status_code == 200
    assert "Print without review" in response.text
    assert _saved(admin_client, "bambuddy_url") is None


def test_requires_csrf(admin_client):
    csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(""))
    assert response.status_code == 403
    assert _saved(admin_client, "bambuddy_url") is None


def test_key_not_echoed_on_error(admin_client, respx_mock):
    respx_mock.get(QUEUE).respond(401)
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token))
    assert KEY not in response.text
    assert URL in response.text  # address and user name stay in the form
    assert 'value="reprintbuddy"' in response.text


def test_invalid_url_is_reported(admin_client):
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token, bambuddy_url="host:8000"))
    assert response.status_code == 200
    assert "must start with http" in response.text
    assert _saved(admin_client, "bambuddy_url") is None


def test_missing_fields_are_reported(admin_client):
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token, bambuddy_api_key=""))
    assert response.status_code == 200
    assert "Please fill in" in response.text


def test_closed_once_configured(ready_client):
    for method in (ready_client.get, ready_client.post):
        response = method("/setup/bambuddy")
        assert response.status_code == 303
        assert response.headers["location"] == "/settings"


def test_key_is_stripped_before_it_is_used_and_saved(admin_client, respx_mock):
    route = respx_mock.get(QUEUE).respond(200, json=[])
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post(
        "/setup/bambuddy", data=_form(token, bambuddy_api_key="  " + KEY + " \r\n")
    )
    assert response.status_code == 303
    assert route.calls.last.request.headers["x-api-key"] == KEY
    assert _saved(admin_client, "bambuddy_api_key") == KEY


@pytest.mark.parametrize("bad", ["bb key", "bbüber", "bb\x01key"])
def test_key_with_unusable_characters_gets_its_own_message(admin_client, respx_mock, bad):
    route = respx_mock.get(QUEUE).respond(200, json=[])
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token, bambuddy_api_key=bad))
    assert response.status_code == 200  # no server error, no misleading "unreachable"
    assert "API key contains characters" in response.text
    assert not route.called
    assert _saved(admin_client, "bambuddy_api_key") is None


@pytest.mark.parametrize("bad", ["http://h:99999", "http://h:abc", "http://h:0", "http://h:-1", "http://:8000", "http://", "https://[::1", "http://h:8000x"])
def test_a_bad_port_is_reported_not_a_server_error(admin_client, respx_mock, bad):
    token = csrf_of(admin_client, "/setup/bambuddy")
    response = admin_client.post("/setup/bambuddy", data=_form(token, bambuddy_url=bad))
    assert response.status_code == 200
    assert "must start with http" in response.text
    assert _saved(admin_client, "bambuddy_url") is None
