"""A running process renders only the templates and texts it started with.

An update replaces the installed files while the old process still answers (it is restarted at
the end). If it read a template or a language file from disk when a page asks for it, it would
mix the old code with the new files, and the page the update shows during the steps would fail.
The tests build the app over a copy of the files in a temporary folder and wreck that copy.
"""

import shutil
from pathlib import Path

import pytest
from conftest import make_client

from reprintbuddy.web import app as app_module
from reprintbuddy import i18n as core_i18n
from reprintbuddy.web import i18n

REAL_TEMPLATES = app_module.TEMPLATES_DIR
REAL_LOCALES = i18n.LOCALES_DIR


@pytest.fixture
def copied_files(tmp_path, monkeypatch):
    """The app built after this reads templates and texts from a copy, which a test may wreck."""
    templates = tmp_path / "copy" / "templates"
    locales = tmp_path / "copy" / "locales"
    shutil.copytree(REAL_TEMPLATES, templates)
    shutil.copytree(REAL_LOCALES, locales)
    monkeypatch.setattr(app_module, "TEMPLATES_DIR", templates)
    monkeypatch.setattr(core_i18n, "LOCALES_DIR", locales)  # where `_read` looks
    i18n._read.cache_clear()  # the texts of a process are read once: this "process" starts now
    yield templates, locales
    i18n._read.cache_clear()  # the next test reads the real texts again


def wreck(templates: Path, locales: Path) -> None:
    """What an update does to the installed files, in its worst form."""
    for path in templates.glob("*.html"):
        path.write_text("{% this is not a template", encoding="utf-8")
    (templates / "settings.html").unlink()
    for path in locales.glob("*.json"):
        path.write_text("not json at all", encoding="utf-8")


def test_the_copy_is_not_the_installed_package(copied_files):
    """The wrecking below must never reach the real files."""
    templates, locales = copied_files
    assert templates != REAL_TEMPLATES and locales != REAL_LOCALES
    assert REAL_TEMPLATES not in templates.parents and REAL_LOCALES not in locales.parents


def test_pages_keep_their_templates_and_texts_when_the_files_change(copied_files, db_path):
    templates, locales = copied_files
    client = make_client(db_path)  # started: templates and texts are in memory now
    wreck(templates, locales)
    response = client.get("/", follow_redirects=True)  # the first setup page, never rendered before
    assert response.status_code == 200
    assert "Create account" in response.text
    assert i18n.translate("en", "nav.overview") == "Overview"


def test_a_page_not_rendered_before_still_comes_from_the_old_files(copied_files, ready_client):
    """The settings page: the update step that reloads it must not run into the new files."""
    templates, locales = copied_files
    wreck(templates, locales)
    for path in ("/settings", "/articles", "/orders"):  # none of them was rendered in this process yet
        response = ready_client.get(path)
        assert response.status_code == 200, path
        assert "Settings" in response.text, path
    assert "Bambuddy" in ready_client.get("/settings").text


def test_the_german_texts_stay_too(copied_files, db_path):
    templates, locales = copied_files
    client = make_client(db_path)
    wreck(templates, locales)
    assert i18n.translate("de", "nav.overview") == "Übersicht"
    assert client.get("/", follow_redirects=True).status_code == 200
