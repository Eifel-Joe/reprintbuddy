"""The stand-in Bambuddy of the documentation pictures grants every right Reprintbuddy requires.

Root: the recording script kept its own copy of the required rights. When 0.4.0 made `settings:read`
required (p342), the copy missed it, the connection check of the pictures failed and the run stopped
after the setup pages. The script now takes the list from the application; this test keeps it so.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from reprintbuddy.bambuddy.connection import REQUIRED_PERMISSIONS
from reprintbuddy.web import app as web_app
from reprintbuddy.web.routes import ops as ops_routes

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def shots(monkeypatch):
    """The recording script as a module; the version patches it makes are taken back after the test."""
    monkeypatch.setattr(web_app, "__version__", web_app.__version__)
    monkeypatch.setattr(ops_routes, "__version__", ops_routes.__version__)
    spec = importlib.util.spec_from_file_location("docs_screenshots_rights", ROOT / "tools" / "docs_screenshots.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # the dataclasses of the script look themselves up there
    spec.loader.exec_module(module)
    return module


def test_the_stand_in_grants_every_required_right(shots):
    granted = set(shots.Mock().permissions)
    assert set(REQUIRED_PERMISSIONS) <= granted, sorted(set(REQUIRED_PERMISSIONS) - granted)
