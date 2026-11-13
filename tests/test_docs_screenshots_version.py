"""The pictures of the documentation do not carry the version of the release (p190).

A step of the version alone must not change a picture: the menu foot and the card "Update" show a
fixed stand-in version, not the one that is installed.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import reprintbuddy
from reprintbuddy.web import app as web_app
from reprintbuddy.web.routes import ops as ops_routes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shots(monkeypatch):
    """The recording script as a module; the version patches it makes are taken back after the test."""
    monkeypatch.setattr(web_app, "__version__", web_app.__version__)
    monkeypatch.setattr(ops_routes, "__version__", ops_routes.__version__)
    spec = importlib.util.spec_from_file_location("docs_screenshots_under_test", ROOT / "tools" / "docs_screenshots.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # the dataclasses of the script look themselves up there
    spec.loader.exec_module(module)
    return module


def test_the_stand_in_is_no_real_version(shots):
    assert re.fullmatch(r"\d+\.\d+\.\d+", shots.SHOT_VERSION)
    assert shots.SHOT_VERSION != reprintbuddy.__version__
    assert shots.SHOT_VERSION.startswith("1.")  # no release of this project looks like it
    assert shots.next_version() != shots.SHOT_VERSION


def test_the_card_update_tells_the_stand_in_not_the_installed_version(shots):
    real = reprintbuddy.__version__
    described = shots.release_runner(["git", "describe", "--tags"]).stdout
    assert f"v{shots.SHOT_VERSION}" in described and real not in described
    body = shots.release_notes_answer("https://example.invalid", {})[1].decode()
    assert f"v{shots.next_version()}" in body and real not in body


def test_the_card_offers_the_next_version_through_the_release_list(shots, tmp_path):
    """v0.4.9: the check reads GitHub's release list; a release counts only with its package."""
    from datetime import UTC, datetime

    from reprintbuddy.ops.releases import check_versions

    (tmp_path / ".git").mkdir()
    info = check_versions(tmp_path, shots.release_runner, shots.release_notes_answer, channel="stable", now=datetime.now(UTC))
    assert (info.installed, info.newest) == (f"v{shots.SHOT_VERSION}", f"v{shots.next_version()}")
    assert info.update_available and info.notes is not None and not info.notes.failed


def test_the_pages_of_the_recorded_application_carry_the_stand_in(shots, tmp_path):
    server = shots.Server(tmp_path, "en")
    try:
        with TestClient(server.app) as client:
            html = client.get("/setup", follow_redirects=True).text
            assert f"?v={shots.SHOT_VERSION}" in html
            assert f"?v={reprintbuddy.__version__}" not in html
        assert server.app.state.version == shots.SHOT_VERSION
        assert ops_routes.__version__ == shots.SHOT_VERSION  # the line "installed ... package ..." of the card
    finally:
        server.app.state.engine.dispose()
