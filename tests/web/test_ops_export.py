import json
import re

from conftest import csrf_of


def test_export_download(ops_env):
    response = ops_env.client.get("/settings/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["cache-control"] == "no-store"
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(
        r'attachment; filename="reprintbuddy-export-\d{8}-\d{6}\.json"', disposition
    ), disposition
    data = json.loads(response.content)
    assert data["format"] == "reprintbuddy-export"
    assert "bb_testkey" not in response.text  # the Bambuddy key set up by the fixture


def test_export_requires_login(ops_env):
    ops_env.client.cookies.clear()
    response = ops_env.client.get("/settings/export")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert "reprintbuddy-export" not in response.text


def test_settings_has_export_link(ops_env):
    assert 'href="/settings/export"' in ops_env.client.get("/settings").text


def test_export_runs_no_commands(ops_env):
    ops_env.client.get("/settings/export")
    assert ops_env.runner.calls == []
