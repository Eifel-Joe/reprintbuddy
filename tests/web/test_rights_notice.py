"""Rights after an update: the first page after a start checks the stored key (Spec 11.3)."""

import httpx
import pytest
from conftest import finish_bambuddy_setup, mock_auth_me

URL = "http://bambuddy.test:8000"
ME = f"{URL}/api/v1/auth/me"


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
