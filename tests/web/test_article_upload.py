"""Uploading a print file from "New article from print file" (Spec 5.1a, v0.3.9 p20)."""

from __future__ import annotations

import asyncio
import html as html_mod
import io
import re
import unicodedata
import zipfile
from datetime import timedelta

import pytest
from conftest import REQUIRED, csrf_of, mock_auth_me

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.routes import upload as upload_routes

PAGE = "/articles/new/upload"
WITH_RIGHT = [*REQUIRED, "library:upload"]


def en(key, **params):
    return translate("en", key, **params)


def zip_bytes(*names: str, extra: bytes = b"") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"data" + extra)
    return buffer.getvalue()


SLICED = zip_bytes("3D/3dmodel.model", "Metadata/plate_1.gcode")
PROJECT = zip_bytes("3D/3dmodel.model")


@pytest.fixture
def up(bb_client, fake_bambuddy, respx_mock):
    """A ready client, the fake Bambuddy (folder 1 "Templates") and a key that may upload."""
    mock_auth_me(respx_mock, permissions=WITH_RIGHT)
    return bb_client


def token(client) -> str:
    return csrf_of(client, "/settings")


def post(client, data=SLICED, name="New part.gcode.3mf", folder=1, **extra):
    query = "" if folder is None else f"?folder={folder}"
    return client.post(
        PAGE + query,
        data={"csrf_token": extra.pop("csrf", None) or token(client)},
        files={"print_file": (name, data, "application/octet-stream")},
        **extra,
    )


def alert(html: str) -> str:
    match = re.search(r'<p class="alert" role="alert">(.*?)</p>', html, re.S)
    return html_mod.unescape(match.group(1)) if match else ""


# ---- the button on the folder view ------------------------------------------------------------


def _button(html):
    return re.search(r'<a class="btn secondary" href="([^"]+)">[^<]*<svg[^>]*><use href="[^"]*#upload"', html)


def test_button_in_the_top_level_and_in_a_folder(up):
    assert _button(up.get("/articles/new").text).group(1) == PAGE
    assert _button(up.get("/articles/new?folder=1").text).group(1) == f"{PAGE}?folder=1"
    assert en("upload.button") in up.get("/articles/new").text


def test_no_button_in_an_external_folder_or_below_it(up, fake_bambuddy):
    mounted = fake_bambuddy.add_folder("Mounted", external=True)
    below = fake_bambuddy.add_folder("Below", mounted.id)
    for folder in (mounted.id, below.id):
        assert _button(up.get(f"/articles/new?folder={folder}").text) is None, folder


def test_no_button_when_choosing_another_file_for_an_article(up, fake_bambuddy):
    from test_archive_pages import make

    article = make(up, name="Template A", library_file_id=1, plate_id=1)
    assert _button(up.get(f"/articles/{article}/relink").text) is None


# ---- the page --------------------------------------------------------------------------------


def test_page_shows_the_form_for_the_folder(up):
    html = up.get(f"{PAGE}?folder=1").text
    assert 'enctype="multipart/form-data"' in html
    assert f'action="{PAGE}?folder=1"' in html
    assert 'name="print_file" type="file" accept=".3mf"' in html
    assert "Templates" in html  # the folder's path in the subtitle
    assert en("upload.file_hint", mb=200) in html


def test_page_without_the_right_shows_both_rights_instead_of_the_form(bb_client, respx_mock):
    mock_auth_me(respx_mock)  # every required right, but not library:upload
    html = bb_client.get(f"{PAGE}?folder=1").text
    assert 'type="file"' not in html
    assert en("upload.no_right") in html
    assert "Upload Library" in html and "<code>library:upload</code>" in html and "Manage Library" in html
    assert bb_client.app.state.rights.status == "ok"  # optional: no notice about the required rights


def test_page_in_an_external_folder_refuses(up, fake_bambuddy):
    mounted = fake_bambuddy.add_folder("Mounted", external=True)
    html = up.get(f"{PAGE}?folder={mounted.id}").text
    assert 'type="file"' not in html
    assert en("upload.external") in alert(html)


@pytest.mark.parametrize("folder", ["99", "x"])
def test_page_for_an_unknown_folder(up, folder):
    response = up.get(f"{PAGE}?folder={folder}")
    assert response.status_code == 404
    assert en("upload.folder_gone") in alert(response.text)


def test_page_without_bambuddy(ready_client):
    assert en("articles.bambuddy_missing") in alert(ready_client.get(PAGE).text)


def test_page_with_bambuddy_unreachable(up, fake_bambuddy):
    fake_bambuddy.unavailable = True
    assert en("articles.bambuddy_error") in alert(up.get(PAGE).text)


def test_page_in_german(up):
    with up.app.state.sessionmaker() as s:
        from reprintbuddy import settings as st

        st.set_setting(s, "language", "de")
    html = up.get(f"{PAGE}?folder=1").text
    assert translate("de", "upload.title") in html and translate("de", "upload.submit") in html


# ---- uploading -------------------------------------------------------------------------------


def test_upload_goes_on_to_the_plate_and_the_article_is_created_as_usual(up, fake_bambuddy):
    response = post(up)
    assert response.status_code == 303
    [(folder, name, data)] = fake_bambuddy.uploaded
    assert (folder, name, data) == (1, "New part.gcode.3mf", SLICED)
    file_id = next(f.id for f in fake_bambuddy._files.values() if f.filename == "New part.gcode.3mf")
    assert response.headers["location"] == f"/articles/new/file/{file_id}"
    # one plate: on to the usual form, where "Create" makes the article (Spec 5.1, 5.1a)
    plate = up.get(response.headers["location"])
    assert plate.status_code in (200, 303)


def test_upload_to_the_top_level(up, fake_bambuddy):
    assert post(up, folder=None).status_code == 303
    assert fake_bambuddy.uploaded[0][0] is None


def test_a_decomposed_name_is_sent_composed(up, fake_bambuddy):
    post(up, name=unicodedata.normalize("NFD", "Größe.gcode.3mf"))
    assert fake_bambuddy.uploaded[0][1] == "Größe.gcode.3mf"


@pytest.mark.parametrize(
    ("name", "data", "code"),
    [
        ("a.stl", SLICED, "not_3mf"),
        ("a|b.3mf", SLICED, "name_invalid"),
        ("dir\\a.3mf", SLICED, "name_invalid"),
        ("a.3mf.", SLICED, "name_invalid"),
        ("ä" * 130 + ".3mf", SLICED, "name_too_long"),
        ("a.3mf", b"not a zip", "not_zip"),
        ("a.3mf", PROJECT, "not_sliced"),
    ],
)
def test_each_check_refuses_before_anything_is_sent(up, fake_bambuddy, name, data, code):
    response = post(up, data=data, name=name)
    assert response.status_code == 400
    assert en(f"upload.error.{code}") in alert(response.text)
    assert fake_bambuddy.uploaded == [] and "upload_file" not in fake_bambuddy.calls
    assert 'type="file"' in response.text  # the form is shown again


def test_no_file_chosen(up, fake_bambuddy):
    response = up.post(f"{PAGE}?folder=1", data={"csrf_token": token(up)})
    assert en("upload.error.no_file") in alert(response.text)
    assert fake_bambuddy.uploaded == []


def test_a_file_over_the_limit_is_refused(up, fake_bambuddy, monkeypatch):
    monkeypatch.setattr(upload_routes, "check_sliced_3mf", _small_limit(len(SLICED) - 1))
    response = post(up)
    assert response.status_code == 413
    assert en("upload.error.too_large") in alert(response.text)
    assert fake_bambuddy.uploaded == []


def _small_limit(limit):
    from reprintbuddy.domain.printfile import check_sliced_3mf

    return lambda file: check_sliced_3mf(file, limit=limit)


def test_a_declared_length_over_the_limit_is_refused_before_the_body_is_read(up, fake_bambuddy, monkeypatch):
    monkeypatch.setattr(upload_routes, "BODY_LIMIT", 100)
    response = post(up)
    assert response.status_code == 413
    assert en("upload.error.too_large") in alert(response.text)
    assert fake_bambuddy.calls == []  # not even the folders were read


def test_without_a_token_nothing_happens(up, fake_bambuddy):
    response = post(up, csrf="forged")
    assert response.status_code == 403
    assert fake_bambuddy.uploaded == [] and fake_bambuddy.calls == []


def test_into_an_external_folder_by_hand_is_refused_before_sending(up, fake_bambuddy):
    mounted = fake_bambuddy.add_folder("Mounted", external=True)
    below = fake_bambuddy.add_folder("Below", mounted.id)
    for folder in (mounted.id, below.id):
        response = post(up, folder=folder)
        assert response.status_code == 409
        assert en("upload.external") in alert(response.text)
    assert fake_bambuddy.uploaded == []


def test_into_an_unknown_folder(up, fake_bambuddy):
    response = post(up, folder=99)
    assert response.status_code == 404
    assert fake_bambuddy.uploaded == []


def test_without_the_right_nothing_is_sent(bb_client, fake_bambuddy, respx_mock):
    mock_auth_me(respx_mock)
    response = post(bb_client)
    assert en("upload.no_right") in response.text
    assert fake_bambuddy.uploaded == []


# ---- the same name ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Template A.gcode.3mf", "TEMPLATE a.GCODE.3MF"])
def test_same_name_is_refused_with_a_link_to_the_existing_file(up, fake_bambuddy, name):
    response = post(up, name=name)
    assert response.status_code == 409
    message = alert(response.text)
    assert en("upload.same_name", name="Template A.gcode.3mf") in message
    assert 'href="/articles/new/file/1"' in message
    assert fake_bambuddy.uploaded == []


def test_same_name_as_a_composed_name(up, fake_bambuddy):
    fake_bambuddy.add_file("Größe.gcode.3mf", 1)
    response = post(up, name=unicodedata.normalize("NFD", "größe.gcode.3mf"))
    assert response.status_code == 409
    assert fake_bambuddy.uploaded == []


def test_same_name_as_an_unsliced_file_has_no_link(up, fake_bambuddy):
    fake_bambuddy.add_file("Draft.3mf", 1, sliced=False)
    response = post(up, name="Draft.3mf")
    message = alert(response.text)
    assert en("upload.same_name_unsliced", name="Draft.3mf") in message
    assert "/articles/new/file/" not in message


def test_same_name_in_another_folder_is_fine(up, fake_bambuddy):
    assert post(up, name="Template A.gcode.3mf", folder=None).status_code == 303


def test_two_uploads_at_once_send_only_one(up, fake_bambuddy):
    """Without JavaScript a double click sends two forms; the lock lets only one pass the name check."""
    fake_bambuddy.delay = 0.05
    app = up.app

    async def both():
        import httpx

        form = {"csrf_token": token(up)}  # first: the session that holds the token is the cookie below
        transport = httpx.ASGITransport(app=app)
        cookies = dict(up.cookies)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=cookies) as http:
            files = {"print_file": ("Twice.gcode.3mf", SLICED, "application/octet-stream")}
            return await asyncio.gather(
                http.post(f"{PAGE}?folder=1", data=form, files=files),
                http.post(f"{PAGE}?folder=1", data=form, files=files),
            )

    first, second = asyncio.run(both())
    assert sorted([first.status_code, second.status_code]) == [303, 409]
    assert len(fake_bambuddy.uploaded) == 1


# ---- after the send --------------------------------------------------------------------------


def test_same_content_gives_a_note_with_the_other_name(up, fake_bambuddy):
    assert post(up, name="First.gcode.3mf").status_code == 303
    response = post(up, name="Second.gcode.3mf")
    assert response.status_code == 303
    page = up.get(response.headers["location"], follow_redirects=True).text
    assert en("upload.duplicate", name="First.gcode.3mf") in page


def test_a_note_about_the_same_content_is_left_out_when_it_cannot_be_read(up, fake_bambuddy):
    post(up, name="First.gcode.3mf")
    fake_bambuddy.fail_next("get_file", BambuddyUnavailable("down"))
    response = post(up, name="Second.gcode.3mf")
    assert response.status_code == 303


def test_bambuddy_does_not_take_it_for_sliced(up, fake_bambuddy, monkeypatch):
    """The check before and Bambuddy disagree: the file is there, no form follows."""
    monkeypatch.setattr(upload_routes, "check_sliced_3mf", lambda file: None)
    response = post(up, data=PROJECT, name="Odd.3mf")
    assert response.status_code == 409
    assert en("upload.bambuddy_not_sliced", name="Odd.3mf") in alert(response.text)
    assert len(fake_bambuddy.uploaded) == 1


@pytest.mark.parametrize(
    ("detail", "key"),
    [
        ("API key does not have 'can_manage_library' permission", "upload.no_right_key"),
        ("API key owner does not have 'library:upload' permission", "upload.no_right_group"),
        ("You can only add to your own folders and folders shared with everyone", "upload.folder_not_writable"),
        ("Cannot upload to a read-only external folder", "upload.folder_not_writable"),
    ],
)
def test_each_refusal_of_bambuddy_names_its_reason(up, fake_bambuddy, detail, key):
    fake_bambuddy.fail_next("upload_file", BambuddyRejected(detail, status=403))
    response = post(up)
    assert response.status_code == 409
    assert en(key) in alert(response.text)


def test_another_refusal_shows_bambuddys_text(up, fake_bambuddy):
    fake_bambuddy.fail_next("upload_file", BambuddyRejected("Filename exceeds 255 bytes", status=400))
    response = post(up)
    assert en("upload.rejected", detail="Filename exceeds 255 bytes") in alert(response.text)


# ---- an unclear answer -----------------------------------------------------------------------


def test_lost_answer_with_the_file_there_goes_on(up, fake_bambuddy):
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("timeout"), after_effect=True)
    response = post(up)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/articles/new/file/")
    assert len(fake_bambuddy.uploaded) == 1  # sent once, never again
    assert fake_bambuddy.calls.count("upload_file") == 1


def test_lost_request_reports_unclear_and_does_not_send_again(up, fake_bambuddy):
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("down"))
    response = post(up)
    assert response.status_code == 502
    assert en("upload.unclear") in alert(response.text)
    assert fake_bambuddy.calls.count("upload_file") == 1


def test_a_file_of_another_user_is_not_taken(up, fake_bambuddy):
    fake_bambuddy.username = "someone-else"
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("timeout"), after_effect=True)
    assert post(up).status_code == 502


async def test_without_the_own_user_nothing_is_taken(fake_bambuddy):
    """Without the stored user no file can be told apart from someone else's (as in `dispatch`)."""
    from datetime import datetime

    await fake_bambuddy.upload_file(1, "New.gcode.3mf", io.BytesIO(SLICED))
    sent_at = datetime(2000, 1, 1)
    found = await upload_routes._find_own(fake_bambuddy, 1, "New.gcode.3mf", len(SLICED), sent_at, "reprintbuddy")
    assert found is not None  # the control: with the user it is found
    for username in (None, ""):
        assert await upload_routes._find_own(fake_bambuddy, 1, "New.gcode.3mf", len(SLICED), sent_at, username) is None


async def test_two_candidates_are_none(fake_bambuddy):
    from datetime import datetime

    await fake_bambuddy.upload_file(1, "New.gcode.3mf", io.BytesIO(SLICED))
    await fake_bambuddy.upload_file(1, "new.gcode.3MF", io.BytesIO(SLICED))
    found = await upload_routes._find_own(fake_bambuddy, 1, "New.gcode.3mf", len(SLICED), datetime(2000, 1, 1), "reprintbuddy")
    assert found is None


def test_a_file_made_before_the_send_is_not_taken(up, fake_bambuddy):
    real_now = fake_bambuddy.now
    fake_bambuddy.now = lambda: real_now() - timedelta(hours=1)
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("timeout"), after_effect=True)
    assert post(up).status_code == 502


def test_a_file_of_another_size_is_not_taken(up, fake_bambuddy, monkeypatch):
    original = fake_bambuddy.upload_file

    async def shorter(folder_id, filename, content):
        return await original(folder_id, filename, io.BytesIO(content.read()[:-1]))

    monkeypatch.setattr(fake_bambuddy, "upload_file", shorter)
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("timeout"), after_effect=True)
    assert post(up).status_code == 502
