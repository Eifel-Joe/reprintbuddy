"""v0.3.14 p169: a file from a folder linked into Bambuddy from outside can be chosen, with a warning.

Bambuddy keeps no checksum for the files of such a folder, so Reprintbuddy would not notice a new version
of the file. The choice stays possible; the pages of the choice say so: the folder list, the plates page
(also on the way to change the print file of an article, where it comes back after a refused choice).
The folder is recognised from `is_external` of the folder list, as the block on uploading does
(`printfile.is_external_folder`: the folder or one above it).
"""

import pytest
from markupsafe import escape
from test_archive_pages import make

from reprintbuddy import settings as st
from reprintbuddy.web.i18n import translate
from conftest import sm_of


def hint(lang="en"):
    return str(escape(translate(lang, "articles.external_hint")))


@pytest.fixture
def external(bb_client, fake_bambuddy):
    """Folder "Mounted" (external) with a file, a folder "Below" under it with a file, and a file in
    "Templates" (id 1, internal) from the fixture."""
    mounted = fake_bambuddy.add_folder("Mounted", external=True)
    below = fake_bambuddy.add_folder("Below", mounted.id)
    outside = fake_bambuddy.add_file("Outside.gcode.3mf", mounted.id)
    deep = fake_bambuddy.add_file("Deep.gcode.3mf", below.id)
    return bb_client, mounted, below, outside, deep


def test_the_folder_list_warns_in_an_external_folder_and_below_it(external):
    client, mounted, below, _outside, _deep = external
    for folder in (mounted.id, below.id):
        assert hint() in client.get(f"/articles/new?folder={folder}").text, folder


def test_the_folder_list_does_not_warn_elsewhere(external):
    client = external[0]
    assert hint() not in client.get("/articles/new").text  # the top level
    assert hint() not in client.get("/articles/new?folder=1").text  # an internal folder


def test_the_files_stay_choosable_in_an_external_folder(external):
    client, mounted, _below, outside, _deep = external
    page = client.get(f"/articles/new?folder={mounted.id}").text
    assert f'href="/articles/new/file/{outside.id}"' in page
    assert client.get(f"/articles/new/file/{outside.id}").status_code == 200


def test_the_plates_page_warns_for_a_file_in_an_external_folder(external):
    client, _mounted, _below, outside, deep = external
    for file in (outside, deep):
        response = client.get(f"/articles/new/file/{file.id}")
        assert response.status_code == 200 and hint() in response.text, file.filename


def test_the_plates_page_does_not_warn_for_an_internal_file(external):
    assert hint() not in external[0].get("/articles/new/file/1").text


def test_changing_the_print_file_of_an_article_warns_on_both_pages(external):
    client, mounted, _below, outside, _deep = external
    article = make(client, name="Template A", library_file_id=1, plate_id=1)
    base = f"/articles/{article}/relink"
    assert hint() in client.get(f"{base}?folder={mounted.id}").text
    assert hint() in client.get(f"{base}/file/{outside.id}").text
    assert hint() not in client.get(f"{base}/file/1").text
    assert hint() not in client.get(f"{base}?folder=1").text


def test_the_warning_is_in_german_in_the_german_interface(external):
    client, mounted, _below, outside, _deep = external
    with sm_of(client).begin() as session:
        st.set_setting(session, "language", "de", commit=False)
    assert hint("de") in client.get(f"/articles/new?folder={mounted.id}").text
    assert hint("de") in client.get(f"/articles/new/file/{outside.id}").text
    assert hint("de") != hint("en")


def test_the_texts_say_what_is_not_noticed_and_where_to_keep_the_files():
    for lang, words in (("en", ("checksum", "new version", "file manager")), ("de", ("Prüfsumme", "neue Fassung", "Dateimanager"))):
        text = translate(lang, "articles.external_hint")
        for word in words:
            assert word in text, (lang, word)


def test_the_docs_no_longer_say_that_nothing_warns():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    text = (root / "docs/help/limits.md").read_text(encoding="utf-8")
    assert "does not warn you about it" not in text
    assert "warns you" in text
    assert "warnt aber davor" in (root / "docs/help/limits.de.md").read_text(encoding="utf-8")
    for name in ("README.md", "README.de.md"):
        assert "warning" in (root / name).read_text(encoding="utf-8").lower() or "Warnung" in (root / name).read_text(encoding="utf-8")


def test_a_failing_folder_list_only_leaves_the_warning_out(external, fake_bambuddy, monkeypatch):
    """The folder list is read for the warning alone. When only that call fails the plates page still
    shows (review of p169); without the folder list there is no way to tell, so no warning."""
    from reprintbuddy.bambuddy.client import BambuddyUnavailable

    client, _mounted, _below, outside, _deep = external

    async def broken():
        raise BambuddyUnavailable("folders")

    monkeypatch.setattr(fake_bambuddy, "list_folders", broken)
    response = client.get(f"/articles/new/file/{outside.id}")
    assert response.status_code == 200
    assert translate("en", "articles.plate") in response.text  # the plates are there
    assert hint() not in response.text
