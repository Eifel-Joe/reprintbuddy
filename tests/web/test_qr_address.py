"""The address inside the QR codes is the server's own network address (Spec 8.4, p116):
found by Reprintbuddy itself; the setting "Address for QR codes" only counts as a special case."""

import io

import pytest
import zxingcpp
from conftest import make_client, finish_admin_setup, finish_bambuddy_setup, sm_of
from factories import make_article
from texts import en
from PIL import Image
from starlette.requests import Request

from reprintbuddy import settings as st
from reprintbuddy.ops.context import OpsContext
from reprintbuddy.ops.server import EffectiveServer
from reprintbuddy.web import netaddr, qr

LAN = "203.0.113.64"


@pytest.fixture
def lan(monkeypatch):
    """The machine sits in a network (the tests otherwise never look at the real one)."""
    monkeypatch.setattr(netaddr, "machine_address", lambda: LAN)


def running(db_path, *, port=8080, tls=False, host="0.0.0.0"):
    client = make_client(
        db_path,
        ops=OpsContext(
            install_dir=db_path.parent / "no-install",
            effective=EffectiveServer(host, port, "digest" if tls else None),
        ),
    )
    finish_bambuddy_setup(finish_admin_setup(client))
    client.app.state.bambuddy_factory = lambda: None
    return client


def new_article(client):
    with sm_of(client).begin() as s:
        return make_article(s, name="Template A").id


def request_with(app, *, server=("testserver", 80), host="testserver", scheme="http"):
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"", "scheme": scheme,
        "server": server, "headers": [(b"host", host.encode())], "app": app,
    }
    return Request(scope)


def decoded(png: bytes) -> str:
    return zxingcpp.read_barcodes(Image.open(io.BytesIO(png)))[0].text


# ---- what the page and the pictures carry ----


def test_the_code_carries_the_servers_own_address_and_port(db_path, lan):
    client = running(db_path, port=8080)
    article = new_article(client)
    page = client.get(f"/articles/{article}", headers={"host": "localhost:8080"}).text
    assert f"http://{LAN}:8080/a/{article}" in page
    assert "localhost:8080/a/" not in page
    assert en("qr.local_warning") not in page


def test_the_picture_says_the_same_address(db_path, lan):
    client = running(db_path, port=8080)
    article = new_article(client)
    png = client.get(f"/articles/{article}/qr.png", headers={"host": "localhost:8080"}).content
    assert decoded(png) == f"http://{LAN}:8080/a/{article}"


def test_https_follows_the_running_server(db_path, lan):
    client = running(db_path, port=8443, tls=True)
    article = new_article(client)
    assert f"https://{LAN}:8443/a/{article}" in client.get(f"/articles/{article}").text


def test_the_entered_address_wins_over_everything(db_path, lan):
    client = running(db_path)
    article = new_article(client)
    with sm_of(client).begin() as s:
        st.set_setting(s, "qr_base_url", "https://shelf.example.com", commit=False)
    page = client.get(f"/articles/{article}").text
    assert f"https://shelf.example.com/a/{article}" in page
    assert LAN not in page


def test_without_a_network_address_the_address_of_the_call_stays_and_the_warning_shows(db_path):
    client = running(db_path)  # `machine_address` is None in the tests (conftest)
    article = new_article(client)
    page = client.get(f"/articles/{article}", headers={"host": "localhost:8080"}).text
    assert f"http://localhost:8080/a/{article}" in page
    assert en("qr.local_warning") in page


# ---- the order of the sources ----


def test_a_specific_listen_address_is_the_servers_address(db_path):
    client = running(db_path, host="203.0.113.9")
    request = request_with(client.app, server=("203.0.113.77", 8080))
    with sm_of(client)() as db:
        assert qr.base_address(request, db) == "http://203.0.113.9:8080"


def test_a_listen_address_on_loopback_is_no_address_for_a_phone(db_path, lan):
    client = running(db_path, host="127.0.0.1")
    with sm_of(client)() as db:
        assert qr.base_address(request_with(client.app), db) == f"http://{LAN}:8080"


def test_the_address_the_connection_came_in_on_beats_the_default_route(db_path, lan):
    client = running(db_path)
    request = request_with(client.app, server=("198.51.100.3", 8080))
    with sm_of(client)() as db:
        assert qr.base_address(request, db) == "http://198.51.100.3:8080"


def test_the_default_route_when_the_connection_was_loopback(db_path, lan):
    client = running(db_path)
    request = request_with(client.app, server=("127.0.0.1", 8080), host="localhost:8080")
    with sm_of(client)() as db:
        assert qr.base_address(request, db) == f"http://{LAN}:8080"


def test_without_a_running_server_context_the_port_of_the_call_is_used(db_path, lan):
    client = make_client(db_path)
    finish_bambuddy_setup(finish_admin_setup(client))
    request = request_with(client.app, host="shelf:9000")
    with sm_of(client)() as db:
        assert qr.base_address(request, db) == f"http://{LAN}:9000"


def test_an_ipv6_address_is_put_in_brackets(db_path):
    client = running(db_path)
    request = request_with(client.app, server=("fd12:3456::7", 8080))
    with sm_of(client)() as db:
        assert qr.base_address(request, db) == "http://[fd12:3456::7]:8080"


# ---- the settings card ----


def test_the_settings_say_what_counts_without_an_entry(db_path, lan):
    client = running(db_path, port=8080)
    page = client.get("/settings?tab=general").text
    assert f"http://{LAN}:8080" in page
    assert en("settings.qr_address_hint") in page
    assert "special case" in en("settings.qr_address_hint").lower()


# ---- the same start of an address without a request: the links of the push messages (Spec 10.6) ----


def test_background_base_is_the_servers_own_address(db_path, lan):
    client = running(db_path, port=8443, tls=True)
    with sm_of(client)() as s:
        assert qr.background_base(client.app, s) == f"https://{LAN}:8443"


def test_background_base_takes_the_entered_address(db_path, lan):
    client = running(db_path)
    with sm_of(client).begin() as s:
        st.set_setting(s, "qr_base_url", "https://shop.example.org/rb", commit=False)
    with sm_of(client)() as s:
        assert qr.background_base(client.app, s) == "https://shop.example.org/rb"


def test_background_base_is_none_without_any_address(db_path):
    client = running(db_path)  # no network address: no link rather than a wrong one
    with sm_of(client)() as s:
        assert qr.background_base(client.app, s) is None
