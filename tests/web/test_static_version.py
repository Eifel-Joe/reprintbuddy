"""The files under /static/ carry the version in their address.

After an update the browser must fetch the new style sheet and scripts, not take the old ones
from its memory: the address of a file changes with every version, so the old copy is never asked for.
"""

import re
from pathlib import Path

from reprintbuddy import __version__
from reprintbuddy.web import i18n

WEB = Path(i18n.__file__).parent
TEMPLATES = sorted((WEB / "templates").glob("*.html"))
STATIC_REFERENCE = re.compile(r"""/static/[^"'?\s)]*""")


def test_there_are_templates_that_reference_static_files():
    """The walk below must not pass because it looked at nothing."""
    assert any(STATIC_REFERENCE.search(path.read_text(encoding="utf-8")) for path in TEMPLATES)


def test_every_static_reference_in_every_template_carries_the_version():
    unversioned = []
    for path in TEMPLATES:
        text = path.read_text(encoding="utf-8")
        for match in STATIC_REFERENCE.finditer(text):
            if not text.startswith("?v={{ app_version }}", match.end()):
                unversioned.append(f"{path.name}: {match.group(0)}")
    assert unversioned == []


def test_style_sheets_and_scripts_do_not_pull_in_further_static_files():
    """A `url(...)` or `@import` of a file under /static/ would need the version too."""
    found = []
    for path in (WEB / "static").rglob("*"):
        if path.suffix in (".css", ".js"):
            text = path.read_text(encoding="utf-8")
            found += [f"{path.name}: {m}" for m in re.findall(r"/static/[^\s\"')]*|@import", text)]
    assert found == []


def test_a_rendered_page_names_the_running_version(ready_client):
    html = ready_client.get("/").text
    assert f"/static/css/reprintbuddy.css?v={__version__}" in html
    assert f"/static/img/logo.svg?v={__version__}" in html
    assert f"/static/img/favicon.svg?v={__version__}" in html
    assert f"/static/icons/icons.svg?v={__version__}#layout-dashboard" in html
    # every /static/ address carries the version; an anchor (#name) may follow it
    addresses = re.findall(r'/static/[^"\s]*', html)
    assert addresses
    assert [a for a in addresses if not re.search(r"\?v=" + re.escape(__version__) + r"(#[\w-]+)?$", a)] == []


def test_the_pages_without_sidebar_name_the_version_too(client):
    html = client.get("/", follow_redirects=True).text  # nothing set up yet: the first setup page
    assert f"/static/img/logo.svg?v={__version__}" in html


def test_the_versioned_address_still_serves_the_file(ready_client):
    response = ready_client.get(f"/static/css/reprintbuddy.css?v={__version__}")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]
