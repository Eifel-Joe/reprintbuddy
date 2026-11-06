"""The running app reads Bambuddy over HTTP with the stored key (B2 Task 8)."""

import sys
from pathlib import Path

import httpx

from conftest import finish_bambuddy_setup
from reprintbuddy.bambuddy.client import HttpBambuddyClient

# The recorded-shape answers live next to the HTTP client tests.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bambuddy"))
from fixtures import API, load  # noqa: E402


def test_ready_client_has_no_bambuddy(ready_client):
    assert ready_client.app.state.bambuddy_factory() is None


def test_app_builds_the_http_client_from_settings(admin_client):
    finish_bambuddy_setup(admin_client)
    client = admin_client.app.state.bambuddy_factory()
    assert isinstance(client, HttpBambuddyClient)


def test_article_page_reads_folders_over_http(admin_client, respx_mock):
    finish_bambuddy_setup(admin_client)
    folders = respx_mock.get(f"{API}/library/folders/").respond(200, json=load("folders.json"))
    files = respx_mock.get(f"{API}/library/files/").respond(200, json=load("files_root.json"))
    response = admin_client.get("/articles/new")
    assert response.status_code == 200
    assert folders.calls.last.request.headers["X-API-Key"] == "bb_testkey"
    assert files.called
    assert "Templates" in response.text
    assert "Template A.gcode.3mf" in response.text


def test_article_page_without_bambuddy(admin_client, respx_mock):
    finish_bambuddy_setup(admin_client)
    respx_mock.get(f"{API}/library/folders/").mock(side_effect=httpx.ConnectError("down"))
    response = admin_client.get("/articles/new")
    assert response.status_code == 200
    assert "Bambuddy cannot be reached" in response.text
