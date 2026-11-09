"""The target quantity as a field in the list and the tiles of the articles (Spec 10.1, p154).

A new target sets the minimum stock to target - buffer (the buffer stays, below the buffer is refused).
Saving is a plain form: enter or the button of the row/tile, no script (Spec 10.5 item 9). The arrows
only change the value in the field; they never send it."""

import re
from html import unescape

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from reprintbuddy.models import Article, utcnow
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def make(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, name=fields.pop("name", "Template A"), **fields).id


def stored(client, a):
    with sm_of(client)() as s:
        row = s.get(Article, a)
        return row.min_stock, row.buffer


def send(client, a, value, *, next_="/articles", page="/articles", token=True, csrf=True, form_token=None):
    data = {"target": value, "next": next_}
    if csrf:
        data["csrf_token"] = csrf_of(client, page)
    if token:
        data["form_token"] = form_token or form_token_of(client, page, f"/articles/{a}/target")
    return client.post(f"/articles/{a}/target", data=data, follow_redirects=False)


def forms_of(html, a):
    return re.findall(rf'<form class="target-form"[^>]*action="/articles/{a}/target".*?</form>', html, re.S)


def field_of(form):
    return re.search(r'<input[^>]*name="target"[^>]*>', form).group(0)


# ---- the field -----------------------------------------------------------------------------------


def test_the_list_row_and_the_tile_each_have_a_field_with_the_current_target(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    html = bb_client.get("/articles").text
    forms = forms_of(html, a)
    assert len(forms) == 2  # the row of the list and the tile
    for form in forms:
        tag = field_of(form)
        assert 'value="15"' in tag and 'inputmode="numeric"' in tag
        assert 'min="5"' in tag  # the arrows stop at the buffer: below it the server refuses
        assert 'type="submit"' in form  # without a script: enter or this button saves
        assert 'name="csrf_token"' in form and 'name="form_token"' in form
        assert 'name="next" value="/articles"' in form
        assert en("articles.set_target", name="Template A") in unescape(form)
    assert en("articles.target_quantity") in html  # the column head stays
    assert html.count('class="bar ') == 2  # and the bar to the target under the field of the row and the tile


def test_the_field_is_in_the_live_region_of_the_list_and_keeps_the_query_of_the_page(bb_client):
    a = make(bb_client, min_stock=1)
    html = bb_client.get("/articles?status=below_min").text
    region = re.search(r'data-live-region="list".*', html, re.S).group(0)
    assert forms_of(region, a)  # a swap of the region never touches a form somebody is changing (live.js)
    assert 'name="next" value="/articles?status=below_min"' in forms_of(html, a)[0]


def test_the_archive_shows_the_number_only(bb_client):
    a = make(bb_client, min_stock=3, buffer=1, archived_at=utcnow())
    html = bb_client.get("/articles?view=archived").text
    assert forms_of(html, a) == [] and 'name="target"' not in html
    assert ">4<" in html


def test_the_label_is_in_german(bb_client):
    from reprintbuddy import settings as st

    a = make(bb_client, min_stock=3)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert "Zielmenge von Template A setzen" in unescape(forms_of(bb_client.get("/articles").text, a)[0])


# ---- saving ---------------------------------------------------------------------------------------


def test_a_new_target_sets_the_minimum_stock_and_goes_back_to_the_page_with_a_notice(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    response = send(bb_client, a, "20", next_="/articles?status=below_min")
    assert response.status_code == 303 and response.headers["location"] == "/articles?status=below_min"
    assert stored(bb_client, a) == (15, 5)
    page = bb_client.get("/articles?status=below_min").text
    assert en("articles.target_saved", name="Template A", target=20) in unescape(page)
    assert 'value="20"' in field_of(forms_of(page, a)[0])


def test_a_target_equal_to_the_buffer_is_allowed(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    assert send(bb_client, a, "5").status_code == 303
    assert stored(bb_client, a) == (0, 5)


def test_a_foreign_next_goes_to_the_list(bb_client):
    a = make(bb_client, min_stock=1)
    response = send(bb_client, a, "3", next_="https://elsewhere.example/x")
    assert response.status_code == 303 and response.headers["location"] == "/articles"


def test_an_unknown_article_is_404(bb_client):
    make(bb_client, min_stock=1)
    token = form_token_of(bb_client, "/articles")  # any form token of the page
    assert send(bb_client, 999, "3", form_token=token).status_code == 404


def test_the_form_needs_the_csrf_token(bb_client):
    a = make(bb_client, min_stock=1)
    assert send(bb_client, a, "3", csrf=False).status_code in (400, 403)
    assert stored(bb_client, a) == (1, 0)


def test_the_same_form_sent_twice_changes_nothing_the_second_time(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    token = form_token_of(bb_client, "/articles", f"/articles/{a}/target")
    assert send(bb_client, a, "20", form_token=token).status_code == 303
    with sm_of(bb_client).begin() as s:
        s.get(Article, a).min_stock = 99  # somebody changed it on another device in between
    again = send(bb_client, a, "20", form_token=token)
    assert again.status_code == 303 and again.headers["location"] == "/articles"
    assert stored(bb_client, a) == (99, 5)  # the repeat did no work
    assert en("form.duplicate") in bb_client.get("/articles").text


# ---- refusals show at the field ----------------------------------------------------------------------


def error_at_field(html, a):
    form = forms_of(html, a)[0]
    found = re.search(r'<p class="field-error" id="([^"]+)" role="alert">([^<]*)</p>', form)
    return form, found


def test_below_the_buffer_is_refused_at_the_field_and_changes_nothing(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    response = send(bb_client, a, "3")
    assert response.status_code == 303 and response.headers["location"] == "/articles"
    assert stored(bb_client, a) == (10, 5)
    page = bb_client.get("/articles").text
    form, found = error_at_field(page, a)
    assert found and unescape(found.group(2)) == en("articles.target_below_buffer", buffer=5)
    tag = field_of(form)
    assert 'value="3"' in tag  # what was typed stays
    assert 'aria-invalid="true"' in tag and f'aria-describedby="{found.group(1)}"' in tag
    assert page.count('class="bar ') == 2  # the bar to the target stays
    assert en("articles.target_saved", name="Template A", target=3) not in unescape(page)
    again, none = error_at_field(bb_client.get("/articles").text, a)
    assert none is None and 'value="15"' in field_of(again)  # shown once; then the real target again


def test_a_buffer_changed_on_another_device_in_the_meantime_is_measured_at_saving(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    token = form_token_of(bb_client, "/articles", f"/articles/{a}/target")
    with sm_of(bb_client).begin() as s:
        s.get(Article, a).buffer = 12  # the page still shows 15 (10 + 5): the target 14 was fine there
    send(bb_client, a, "11", form_token=token)
    assert stored(bb_client, a) == (10, 12)  # 11 < 12: refused, nothing negative stored
    _, found = error_at_field(bb_client.get("/articles").text, a)
    assert found and unescape(found.group(2)) == en("articles.target_below_buffer", buffer=12)


def test_no_number_is_refused_at_the_field(bb_client):
    a = make(bb_client, min_stock=10, buffer=5)
    for bad in ("", "abc", "-1", "1.5", "99999999999999999999"):
        assert send(bb_client, a, bad).status_code == 303
        assert stored(bb_client, a) == (10, 5)
        _, found = error_at_field(bb_client.get("/articles").text, a)
        assert found, bad


def test_an_archived_article_is_refused_without_an_error_page(bb_client):
    a = make(bb_client, min_stock=3, archived_at=utcnow())
    page = "/articles?view=archived"
    response = send(bb_client, a, "9", next_=page, page=page, form_token=form_token_of(bb_client, page))
    assert response.status_code == 303 and stored(bb_client, a) == (3, 0)
    assert bb_client.get("/articles?view=archived").status_code == 200
    assert bb_client.get("/articles").status_code == 200  # the stored error does not outlive the next list
