"""Choosing a print file: list or tiles, a preview image per file, only sliced files (Task 11)."""

import json
import re
import ssl
from pathlib import Path

import httpx
import pytest
from test_archive_pages import act, make

from conftest import csrf_of, finish_bambuddy_setup, form_token_of
from reprintbuddy.bambuddy.client import BambuddyRejected
from reprintbuddy.web.i18n import translate

NOT_SLICED_EN = "Not sliced – slice it first and put the sliced file (.gcode.3mf) into Bambuddy."
NOT_SLICED_DE = "Nicht geslict – erst slicen und die geslicte Datei (.gcode.3mf) in Bambuddy ablegen."
THUMB = "/articles/new/file/{id}/thumbnail"


def _section(html, cls):
    """The markup of the table (`table-card`) or of the tiles (`tiles`) of the collection."""
    split = html.index('<div class="tiles">')
    return html[split:] if cls == "tiles" else html[html.index("table-card") : split]


def _images(html):
    return re.findall(r'<img class="([^"]+)" src="([^"]+)" alt="" loading="lazy"(?: onerror="[^"]*")?>', html)


# ---- locale ------------------------------------------------------------------------


def test_not_sliced_texts_are_final():
    assert translate("en", "articles.not_sliced") == NOT_SLICED_EN
    assert translate("de", "articles.not_sliced") == NOT_SLICED_DE


def test_new_keys_are_appended_in_every_locale_file():
    folder = Path(__file__).resolve().parents[2] / "src" / "reprintbuddy" / "locales"
    for name in ("en.json", "de.json"):
        keys = list(json.loads((folder / name).read_text(encoding="utf-8")))
        assert keys[-2:] == ["articles.not_sliced", "articles.relink_subtitle"], name


def test_relink_subtitle_texts_are_final():
    assert translate("de", "articles.relink_subtitle") == "Eine andere Druckdatei für diesen Artikel wählen"
    assert translate("en", "articles.relink_subtitle") == "Choose another print file for this article"


def _sub(html):
    return re.search(r'<p class="page-sub">(.*?)</p>', html, re.S).group(1)


def test_relink_pages_have_the_relink_subtitle_and_create_pages_keep_theirs(mixed, fake_bambuddy):
    a = make(mixed, name="Template A", library_file_id=1, plate_id=1)
    act(mixed, f"/articles/{a}", "archive")
    relink = translate("en", "articles.relink_subtitle")
    create = translate("en", "articles.new_subtitle")
    for path, status in [(f"/articles/{a}/relink", 200), (f"/articles/{a}/relink?folder=1", 200),
                         (f"/articles/{a}/relink/file/4", 409), (f"/articles/{a}/relink/file/99", 404)]:
        response = mixed.get(path)
        assert response.status_code == status, path
        assert _sub(response.text) == relink, path
    fake_bambuddy.unavailable = True  # the problem page of the browse step
    response = mixed.get(f"/articles/{a}/relink")
    assert _sub(response.text) == relink
    fake_bambuddy.unavailable = False
    # the POST of the relink path ends on the same pages
    path = f"/articles/{a}/relink/file/4/plate/1"
    data = {"csrf_token": csrf_of(mixed, "/articles"), "form_token": form_token_of(mixed, "/articles/new/file/1/plate/1")}
    response = mixed.post(path, data=data)
    assert response.status_code == 409 and _sub(response.text) == relink
    for path, status in [("/articles/new", 200), ("/articles/new?folder=1", 200), ("/articles/new/file/4", 409),
                         ("/articles/new/file/99", 404)]:
        response = mixed.get(path)
        assert response.status_code == status, path
        assert _sub(response.text) == create, path


# ---- the two layouts -----------------------------------------------------------------


@pytest.fixture
def mixed(bb_client, fake_bambuddy):
    """Folder 1 holds Template A (1), Template B (2), one sliced without image (3) and unsliced ones (4-6)."""
    fake_bambuddy.add_file("Template C.gcode.3mf", 1, has_thumbnail=False)  # id 3
    fake_bambuddy.add_file("Template D.3mf", 1, sliced=False)  # id 4
    fake_bambuddy.add_file("Template E.stl", 1, sliced=False)  # id 5
    fake_bambuddy.add_file("Template F.gcode", 1, sliced=False)  # id 6
    return bb_client


@pytest.mark.parametrize("layout", ["list", "tiles"])
def test_every_file_has_an_image_or_the_placeholder(mixed, layout):
    mixed.cookies.set("rb_layout_files", layout)
    html = mixed.get("/articles/new?folder=1").text
    assert f"layout-{layout}" in html
    for part in ("table-card", "tiles"):
        section = _section(html, part)
        sources = [src for _cls, src in _images(section)]
        assert THUMB.format(id=1) in sources and THUMB.format(id=2) in sources
        assert THUMB.format(id=3) not in sources  # no image: no request, the placeholder
        assert THUMB.format(id=4) not in sources  # unsliced files are not shown at all
        assert "thumb-placeholder" in section
    small = [cls for cls, src in _images(_section(html, "table-card")) if "/file/" in src]
    large = [cls for cls, src in _images(_section(html, "tiles")) if "/file/" in src]
    assert small and all("thumb-sm" in cls for cls in small)
    assert large and all("thumb-lg" in cls for cls in large)


@pytest.mark.parametrize("layout", ["list", "tiles"])
def test_preview_covers_its_placeholder_and_gives_way_when_it_fails(mixed, layout):
    """A loaded picture has an opaque background; a failed one removes itself (placeholder shows)."""
    mixed.cookies.set("rb_layout_files", layout)
    html = mixed.get("/articles/new?folder=1").text
    imgs = re.findall(r"<img [^>]*/file/\d+/thumbnail[^>]*>", _section(html, "table-card" if layout == "list" else "tiles"))
    assert imgs and all('onerror="this.remove()"' in tag for tag in imgs)
    css = Path(__file__).resolve().parents[2] / "src/reprintbuddy/web/static/css/reprintbuddy.css"
    rule = re.search(r"\.thumb-box > img \{([^}]*)\}", css.read_text(encoding="utf-8")).group(1)
    assert "background: var(--bg)" in rule and "background: none" not in rule


@pytest.mark.parametrize("layout", ["list", "tiles"])
def test_long_file_names_wrap_inside_tile_and_row(mixed, layout):
    mixed.cookies.set("rb_layout_files", layout)
    html = mixed.get("/articles/new?folder=1").text
    section = _section(html, "table-card" if layout == "list" else "tiles")
    for name in ("Template A", "Template B", "Template C"):
        assert re.search(rf'<div class="file-name">[^<]*(?:<a [^>]*>)?{name}', section), name
    css = Path(__file__).resolve().parents[2] / "src/reprintbuddy/web/static/css/reprintbuddy.css"
    rule = re.search(r"\.file-name \{([^}]*)\}", css.read_text(encoding="utf-8")).group(1)
    assert "overflow-wrap: anywhere" in rule and "min-width: 0" in rule


def test_default_layout_holds_both(mixed):
    html = mixed.get("/articles/new?folder=1").text
    assert "layout-auto" in html and "table-card" in html and 'class="tiles"' in html


@pytest.mark.parametrize("layout", ["list", "tiles"])
def test_folders_get_the_folder_icon_not_an_image(mixed, layout):
    mixed.cookies.set("rb_layout_files", layout)
    html = mixed.get("/articles/new").text  # the root holds the folder "Templates"
    for part in ("table-card", "tiles"):
        section = _section(html, part)
        assert "icons.svg" in section and "#folder" in section
        assert "/articles/new?folder=1" in section
        assert [src for _cls, src in _images(section) if "/file/" in src] == []


def test_toggle_is_remembered(mixed):
    page = mixed.get("/articles/new?folder=1").text
    assert 'class="layout-toggle"' in page
    switched = mixed.get("/articles/new?folder=1&layout=tiles")
    assert switched.status_code == 303 and "layout" not in switched.headers["location"]
    assert "folder=1" in switched.headers["location"]
    assert "rb_layout_files=tiles" in switched.headers["set-cookie"]
    assert "layout-tiles" in mixed.get("/articles/new?folder=1").text
    mixed.get("/articles/new?folder=1&layout=list")
    assert "layout-list" in mixed.get("/articles/new?folder=1").text


def test_relink_browse_has_toggle_and_images(mixed):
    a = make(mixed, name="Template A", library_file_id=1, plate_id=1)
    act(mixed, f"/articles/{a}", "archive")
    mixed.cookies.set("rb_layout_files", "tiles")
    html = mixed.get(f"/articles/{a}/relink?folder=1").text
    assert "layout-tiles" in html and 'class="layout-toggle"' in html
    assert f"/articles/new/file/1/thumbnail" in html


def test_rendering_the_list_asks_for_no_image(mixed, fake_bambuddy):
    mixed.get("/articles/new?folder=1")
    assert "file_thumbnail" not in fake_bambuddy.calls


# ---- the image route -------------------------------------------------------------------


def test_image_route_returns_bytes_and_type(bb_client):
    response = bb_client.get(THUMB.format(id=1))
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, max-age=300"
    assert response.content.startswith(b"\x89PNG")


def test_image_route_keeps_the_content_type_of_bambuddy(bb_client, fake_bambuddy):
    async def jpeg(file_id):
        return "image/jpeg", b"\xff\xd8\xff\xe0abc"

    fake_bambuddy.file_thumbnail = jpeg
    response = bb_client.get(THUMB.format(id=1))
    assert response.headers["content-type"] == "image/jpeg" and response.content == b"\xff\xd8\xff\xe0abc"


def test_image_route_404_on_bambuddy_error(bb_client, fake_bambuddy):
    fake_bambuddy.fail_next("file_thumbnail", BambuddyRejected("boom"))
    assert bb_client.get(THUMB.format(id=1)).status_code == 404
    fake_bambuddy.unavailable = True
    assert bb_client.get(THUMB.format(id=1)).status_code == 404


def test_image_route_404_for_unknown_file_and_without_client(bb_client):
    assert bb_client.get(THUMB.format(id=99)).status_code == 404
    bb_client.app.state.bambuddy_factory = lambda: None
    assert bb_client.get(THUMB.format(id=1)).status_code == 404


# ---- twenty images, one TLS context --------------------------------------------------------


def test_twenty_images_build_no_new_tls_context(admin_client, respx_mock, monkeypatch):
    finish_bambuddy_setup(admin_client)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    for n in range(1, 21):
        respx_mock.get(f"http://bambuddy.test:8000/api/v1/library/files/{n}/thumbnail").respond(
            200, content=png, headers={"content-type": "image/png"}
        )
    built, loaded = [], []
    original_build, original_load = httpx.create_ssl_context, ssl.create_default_context

    def count_build(*a, **k):
        built.append(1)
        return original_build(*a, **k)

    def count_load(*a, **k):
        loaded.append(1)
        return original_load(*a, **k)

    monkeypatch.setattr(httpx, "create_ssl_context", count_build)
    monkeypatch.setattr(ssl, "create_default_context", count_load)
    for n in range(1, 21):
        assert admin_client.get(THUMB.format(id=n)).status_code == 200
    assert built == [] and loaded == []  # the context made at start is reused


# ---- only sliced files ---------------------------------------------------------------------


def _names(section):
    return re.findall(r'<div class="file-name">\s*(?:<a [^>]*>)?([^<]+)', section)


@pytest.mark.parametrize("layout", ["list", "tiles"])
def test_only_sliced_files_and_folders_are_listed(mixed, fake_bambuddy, layout):
    mixed.cookies.set("rb_layout_files", layout)
    fake_bambuddy.add_file("Old.gcode.3mf", 1)  # id 7, an old entry that is sliced by its name
    fake_bambuddy.add_folder("Sub", 1)
    html = mixed.get("/articles/new?folder=1").text
    for part in ("table-card", "tiles"):
        section = _section(html, part)
        names = sorted(n.strip() for n in _names(section))
        assert names == ["Old.gcode.3mf", "Template A.gcode.3mf", "Template B.gcode.3mf", "Template C.gcode.3mf"]
        for hidden in ("Template D", "Template E", "Template F", "Not sliced", "Nicht geslict"):
            assert hidden not in section
        assert ">Sub<" in section
        assert 'href="/articles/new/file/1"' in section and 'href="/articles/new/file/7"' in section
        for hidden_id in (4, 5, 6):
            assert f"/articles/new/file/{hidden_id}" not in section
    assert NOT_SLICED_EN not in html


def test_both_layouts_list_the_same_names(mixed):
    html = mixed.get("/articles/new?folder=1").text
    assert sorted(_names(_section(html, "table-card"))) == sorted(_names(_section(html, "tiles")))
    assert len(_names(_section(html, "tiles"))) == 3


def test_no_request_for_hidden_files(mixed, fake_bambuddy):
    html = mixed.get("/articles/new?folder=1").text
    for hidden_id in (4, 5, 6):
        assert THUMB.format(id=hidden_id) not in html
    assert "file_thumbnail" not in fake_bambuddy.calls


def test_relink_browse_hides_unsliced_files_too(mixed):
    a = make(mixed, name="Template A", library_file_id=1, plate_id=1)
    act(mixed, f"/articles/{a}", "archive")
    html = mixed.get(f"/articles/{a}/relink?folder=1").text
    assert "Template A" in html and "Template D" not in html and "Template E" not in html


def test_folder_without_sliced_files_shows_the_empty_state(bb_client, fake_bambuddy):
    folder = fake_bambuddy.add_folder("Raw")
    fake_bambuddy.add_file("Only.3mf", folder.id, sliced=False)
    html = bb_client.get(f"/articles/new?folder={folder.id}").text
    assert translate("en", "articles.no_files") in html
    assert "Only.3mf" not in html


def _call(client, fake, method, path, **kw):
    fake.calls.clear()
    response = client.request(method, path, **kw)
    return response, list(fake.calls)


def test_plates_url_of_unsliced_file_shows_message_only(mixed, fake_bambuddy):
    response, calls = _call(mixed, fake_bambuddy, "GET", "/articles/new/file/4")
    assert response.status_code == 409 and NOT_SLICED_EN in response.text
    assert calls == ["get_file"]


def test_plate_form_url_of_unsliced_file_shows_message_only(mixed, fake_bambuddy):
    response, calls = _call(mixed, fake_bambuddy, "GET", "/articles/new/file/4/plate/1")
    assert response.status_code == 409 and NOT_SLICED_EN in response.text
    assert calls == ["get_file"]


def test_create_post_of_unsliced_file_is_refused(mixed, fake_bambuddy):
    path = "/articles/new/file/1/plate/1"
    data = {"csrf_token": csrf_of(mixed, "/articles"), "form_token": form_token_of(mixed, path, path),
            "name": "X", "units_per_print": "1", "min_stock": "0", "buffer": "0", "target": "model:A1"}
    response, calls = _call(mixed, fake_bambuddy, "POST", "/articles/new/file/4/plate/1", data=data)
    assert response.status_code == 409 and NOT_SLICED_EN in response.text
    assert calls == ["get_file"]


def test_relink_urls_of_unsliced_file_are_refused(mixed, fake_bambuddy):
    a = make(mixed, name="Template A", library_file_id=1, plate_id=1)
    act(mixed, f"/articles/{a}", "archive")
    response, calls = _call(mixed, fake_bambuddy, "GET", f"/articles/{a}/relink/file/4")
    assert response.status_code == 409 and NOT_SLICED_EN in response.text
    assert calls == ["get_file"]
    path = f"/articles/{a}/relink/file/4/plate/1"
    data = {"csrf_token": csrf_of(mixed, "/articles"), "form_token": form_token_of(mixed, "/articles/new/file/1/plate/1")}
    response, calls = _call(mixed, fake_bambuddy, "POST", path, data=data)
    assert response.status_code == 409 and NOT_SLICED_EN in response.text
    assert calls == ["get_file"]


def test_sliced_files_still_work(mixed):
    assert mixed.get("/articles/new/file/1").status_code == 200
    assert mixed.get("/articles/new/file/1/plate/1").status_code == 200
    assert mixed.get("/articles/new/file/3/plate/1").status_code == 200  # no image, still sliced


# ---- the new look: head, toolbar, buttons ------------------------------------------------


def _one_head(html, icon):
    assert html.count('class="page-head"') == 1 and len(re.findall(r"<h1[\s>]", html)) == 1
    found = re.search(r'<header class="page-head"><span class="page-icon">.*?#([a-z0-9-]+)"', html, re.S)
    assert found and found.group(1) == icon


def test_file_choice_has_head_toolbar_and_a_back_button_like_the_other_lists(mixed):
    html = mixed.get("/articles/new?folder=1").text
    _one_head(html, "package")
    assert "page-sub" in html and "Create an article from a print file" in html
    toolbar = html[html.index('<div class="toolbar">') : html.index('<div class="collection')]
    assert 'class="crumbs"' in toolbar and 'class="layout-toggle"' in toolbar
    assert re.search(r'<div class="toolbar-actions"><a class="btn secondary" href="/articles">', toolbar)
    assert 'class="btn-link" href' not in html


def test_relink_file_choice_has_the_same_look(mixed):
    a = make(mixed, name="Template A", library_file_id=1, plate_id=1)
    act(mixed, f"/articles/{a}", "archive")
    html = mixed.get(f"/articles/{a}/relink?folder=1").text
    _one_head(html, "package")
    assert 'class="toolbar-actions"' in html and 'class="btn-link" href' not in html


def test_empty_state_is_a_card(mixed, fake_bambuddy):
    html = mixed.get("/articles/new?folder=1").text
    assert 'class="btn-link" href' not in html
    empty = mixed.get("/articles/new?folder=999").text
    assert '<div class="card"><p class="muted">' in empty and 'class="collection' not in empty


def test_plate_step_has_head_and_back_button(mixed):
    html = mixed.get("/articles/new/file/1").text
    _one_head(html, "package")
    assert 'class="toolbar-actions"><a class="btn secondary"' in html and 'class="btn-link" href' not in html


def test_plate_step_back_button_has_its_symbol(mixed):
    html = mixed.get("/articles/new/file/1").text
    assert re.search(r'<a class="btn secondary" href="[^"]*">.*?#chevrons-left"', html, re.S)


def test_not_sliced_note_is_a_card_with_head_and_button(mixed):
    response = mixed.get("/articles/new/file/4")
    assert response.status_code == 409
    html = response.text
    _one_head(html, "circle-alert")
    assert '<div class="card"><p class="alert" role="alert">' + NOT_SLICED_EN in html
    assert 'class="btn secondary" href="/articles"' in html and 'class="btn-link" href' not in html


def test_problem_card_keeps_a_gap_to_the_button_below():
    """The card sits in a `section` (its bottom margin is the gap), the button in the toolbar below it."""
    template = Path(__file__).resolve().parents[2] / "src" / "reprintbuddy" / "web" / "templates" / "article_browse.html"
    text = template.read_text(encoding="utf-8")
    assert re.search(r'<div class="section"><div class="card"><p class="alert" role="alert">\{\{ t\(problem\) \}\}</p></div></div>\s*<div class="toolbar">', text)
    css = (template.parents[1] / "static" / "css" / "reprintbuddy.css").read_text(encoding="utf-8")
    assert re.search(r"^\.section \{ margin-bottom: 1\.25rem; \}", css, re.M)


def test_not_sliced_page_wraps_card_in_section(mixed):
    html = mixed.get("/articles/new/file/4").text
    assert '<div class="section"><div class="card"><p class="alert" role="alert">' + NOT_SLICED_EN in html
    assert re.search(r'</div></div>\s*<div class="toolbar"><div class="toolbar-actions"><a class="btn secondary" href="/articles"', html)
