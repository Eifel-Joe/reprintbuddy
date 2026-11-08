"""Uploading a new version of the print file while changing the file of an article (Spec 5.1, 5.1a, v0.3.10 p134).

The route and every rule are those of "New article from print file" (`test_article_upload.py`);
only the target differs: `?article=<id>`. After the upload it goes to the plates of this article.
"""

from __future__ import annotations

import html as html_mod
import re

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from test_article_upload import PAGE, SLICED, alert, en, mock_auth_me, token, up  # noqa: F401

from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.models import Article, utcnow
from reprintbuddy.web.routes import upload as upload_routes


@pytest.fixture
def art(up):  # noqa: F811
    with sm_of(up).begin() as s:
        return make_article(s, name="Template A", library_file_id=1, plate_id=1, target_model="A1", stock=4).id


def url(article, folder=1):
    parts = [] if folder is None else [f"folder={folder}"]
    return f"{PAGE}?" + "&".join([*parts, f"article={article}"])


def post(client, article, name="New part.gcode.3mf", folder=1, data=SLICED, csrf=None):
    return client.post(
        url(article, folder),
        data={"csrf_token": csrf or token(client)},
        files={"print_file": (name, data, "application/octet-stream")},
    )


def plain(response) -> str:
    return html_mod.unescape(response.text)


# ---- the button in the choice of another file -------------------------------------------------


def _button(html):
    return re.search(r'<a class="btn secondary" href="([^"]+)">[^<]*<svg[^>]*><use href="[^"]*#upload"', html)


def test_button_in_the_choice_of_another_file(up, art):
    assert html_mod.unescape(_button(up.get(f"/articles/{art}/relink").text).group(1)) == url(art, None)
    assert html_mod.unescape(_button(up.get(f"/articles/{art}/relink?folder=1").text).group(1)) == url(art, 1)
    assert en("upload.button") in up.get(f"/articles/{art}/relink").text


def test_button_also_for_an_archived_article(up, art):
    with sm_of(up).begin() as s:
        s.get(Article, art).archived_at = utcnow()
    assert _button(up.get(f"/articles/{art}/relink?folder=1").text) is not None


def test_no_button_in_an_external_folder_of_the_choice(up, art, fake_bambuddy):
    mounted = fake_bambuddy.add_folder("Mounted", external=True)
    below = fake_bambuddy.add_folder("Below", mounted.id)
    for folder in (mounted.id, below.id):
        assert _button(up.get(f"/articles/{art}/relink?folder={folder}").text) is None, folder


def test_the_new_article_choice_keeps_its_own_button(up, art):
    assert _button(up.get("/articles/new?folder=1").text).group(1) == f"{PAGE}?folder=1"


# ---- the page --------------------------------------------------------------------------------


def test_page_keeps_folder_and_article_and_names_the_article(up, art):
    html = up.get(url(art)).text
    assert f'action="{url(art)}"' in plain(up.get(url(art)))
    assert en("upload.title_relink", name="Template A") in html_mod.unescape(html)
    assert 'name="print_file" type="file"' in html
    assert f'href="/articles/{art}/relink?folder=1"' in html  # back: the choice of this article, same folder
    assert 'href="/articles/new' not in html


def test_page_in_german(up, art):
    from reprintbuddy import settings as st

    with sm_of(up).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert "Druckdatei ändern für Template A" in plain(up.get(url(art)))


def test_the_article_name_is_escaped_once(up):
    with sm_of(up).begin() as s:
        a = make_article(s, name="A&B <x>", library_file_id=1, plate_id=1).id
    html = up.get(url(a)).text
    assert "A&amp;B &lt;x&gt;" in html and "&amp;amp;" not in html and "<x>" not in html


@pytest.mark.parametrize("article", ["999", "x", "", "0", "-1"])
def test_page_for_an_unknown_article_is_the_error_page(up, fake_bambuddy, article):
    response = up.get(f"{PAGE}?folder=1&article={article}")
    assert response.status_code == 404
    assert en("articles.not_found") in alert(response.text)
    assert 'type="file"' not in response.text
    assert 'href="/articles/new' not in response.text  # not back to creating a new one
    assert fake_bambuddy.calls == []


def test_page_without_the_right_and_external_folder_keep_the_way_back(bb_client, respx_mock, fake_bambuddy):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
    mock_auth_me(respx_mock)  # no library:upload
    response = bb_client.get(url(a))
    assert en("upload.no_right") in response.text and f'href="/articles/{a}/relink?folder=1"' in response.text
    mounted = fake_bambuddy.add_folder("Mounted", external=True)
    response = bb_client.get(url(a, mounted.id))
    assert en("upload.external") in alert(response.text)
    assert f'href="/articles/{a}/relink?folder={mounted.id}"' in response.text


# ---- uploading -------------------------------------------------------------------------------


def test_upload_goes_to_the_plates_of_this_article_and_the_file_is_changed_as_usual(up, art, fake_bambuddy):
    response = post(up, art)
    assert response.status_code == 303
    assert [(f, n) for f, n, _ in fake_bambuddy.uploaded] == [(1, "New part.gcode.3mf")]
    file_id = next(f.id for f in fake_bambuddy._files.values() if f.filename == "New part.gcode.3mf")
    assert response.headers["location"] == f"/articles/{art}/relink/file/{file_id}"
    with sm_of(up)() as s:  # the article is not touched before a plate is chosen
        row = s.get(Article, art)
        assert (row.library_file_id, row.stock) == (1, 4)
    page = f"/articles/{art}/relink/file/{file_id}"
    assert up.get(page).status_code == 200
    path = f"{page}/plate/1"
    done = up.post(path, data={"csrf_token": csrf_of(up, page), "form_token": form_token_of(up, page, path)})
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{art}"
    with sm_of(up)() as s:
        row = s.get(Article, art)
        assert (row.library_file_id, row.file_name, row.stock) == (file_id, "New part.gcode.3mf", 4)


def test_upload_for_an_archived_article(up, art):
    with sm_of(up).begin() as s:
        s.get(Article, art).archived_at = utcnow()
    assert post(up, art).headers["location"].startswith(f"/articles/{art}/relink/file/")


def test_upload_to_the_top_level(up, art, fake_bambuddy):
    assert post(up, art, folder=None).status_code == 303
    assert fake_bambuddy.uploaded[0][0] is None


@pytest.mark.parametrize("article", ["999", "x"])
def test_upload_for_an_unknown_article_sends_nothing(up, fake_bambuddy, article):
    response = up.post(
        f"{PAGE}?folder=1&article={article}",
        data={"csrf_token": token(up)},
        files={"print_file": ("New part.gcode.3mf", SLICED, "application/octet-stream")},
    )
    assert response.status_code == 404 and en("articles.not_found") in alert(response.text)
    assert fake_bambuddy.uploaded == []
    assert "upload_file" not in fake_bambuddy.calls and "list_files" not in fake_bambuddy.calls


def test_refusals_show_the_form_again_for_this_article(up, art, fake_bambuddy):
    response = post(up, art, name="a.stl")
    assert response.status_code == 400
    assert en("upload.error.not_3mf") in alert(response.text)
    assert f'action="{url(art)}"' in plain(response)
    assert fake_bambuddy.uploaded == []


def test_same_name_links_to_choosing_the_existing_file_for_this_article(up, art, fake_bambuddy):
    response = post(up, art, name="TEMPLATE a.gcode.3mf")
    assert response.status_code == 409
    assert en("upload.same_name", name="Template A.gcode.3mf") in alert(response.text)
    assert f'href="/articles/{art}/relink/file/1"' in response.text
    assert en("upload.use_existing_relink") in plain(response)
    assert "/articles/new/file/" not in response.text
    assert fake_bambuddy.uploaded == []


def test_the_link_of_a_new_article_is_unchanged(up, fake_bambuddy):
    response = up.post(
        f"{PAGE}?folder=1",
        data={"csrf_token": token(up)},
        files={"print_file": ("Template A.gcode.3mf", SLICED, "application/octet-stream")},
    )
    assert 'href="/articles/new/file/1"' in response.text and en("upload.use_existing") in plain(response)


def test_lost_answer_with_the_file_there_goes_on_with_this_article(up, art, fake_bambuddy):
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("timeout"), after_effect=True)
    response = post(up, art)
    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/articles/{art}/relink/file/")
    assert fake_bambuddy.calls.count("upload_file") == 1


def test_lost_request_does_not_send_again_and_leads_back_to_the_choice(up, art, fake_bambuddy):
    fake_bambuddy.fail_next("upload_file", BambuddyUnavailable("down"))
    response = post(up, art)
    assert response.status_code == 502 and en("upload.unclear") in alert(response.text)
    assert 'type="file"' not in response.text
    assert f'href="/articles/{art}/relink?folder=1"' in response.text
    assert fake_bambuddy.calls.count("upload_file") == 1


def test_same_content_note_and_the_way_on(up, art, fake_bambuddy):
    assert post(up, art, name="First.gcode.3mf").status_code == 303
    response = post(up, art, name="Second.gcode.3mf")
    assert response.headers["location"].startswith(f"/articles/{art}/relink/file/")
    page = up.get(response.headers["location"], follow_redirects=True).text
    assert en("upload.duplicate", name="First.gcode.3mf") in page


# ---- the protection of v0.3.9 holds here too ---------------------------------------------------


def test_without_a_token_nothing_happens(up, art, fake_bambuddy):
    response = post(up, art, csrf="forged")
    assert response.status_code == 403
    assert fake_bambuddy.uploaded == [] and fake_bambuddy.calls == []


def test_a_declared_length_over_the_limit_is_refused_before_the_body_is_read(up, art, fake_bambuddy, monkeypatch):
    monkeypatch.setattr(upload_routes, "BODY_LIMIT", 100)
    response = post(up, art)
    assert response.status_code == 413 and en("upload.error.too_large") in alert(response.text)
    assert fake_bambuddy.calls == [] and response.headers.get("connection") == "close"
    assert 'type="file"' not in response.text
    assert f'href="/articles/{art}/relink?folder=1"' in response.text  # no database read, but the way back is this article's


def test_body_limit_is_that_of_the_upload_path(up):
    assert up.app.state.body_limits[PAGE] == upload_routes.BODY_LIMIT  # `?article=` is a query, not another path
