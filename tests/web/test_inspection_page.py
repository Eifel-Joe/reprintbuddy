"""Page "To check" (Spec 8.2, 10.3, 12): finished prints waiting for the count of good pieces."""

import html
import re

import pytest
from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_article, make_dispatch, make_entry, make_part
from texts import de, en
from reprintbuddy import settings as st
from reprintbuddy.domain import inspection, rules, tracking
from reprintbuddy.models import Article, ArticlePart, Inspection, JournalEntry

PAGE = "/inspection"
_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def finished(client, name="Template A", units=1, *, parts=(), **fields):
    """An article with a finished print that waits for inspection; returns (article id, inspection id).

    `parts` are (name, qty_per_unit, stock) of bought parts built into the article.
    """
    with sm_of(client).begin() as s:
        article = s.scalar(select(Article).where(Article.name == name)) or make_article(s, name=name, **fields)
        for part_name, qty, part_stock in parts:
            part = make_part(s, name=part_name, stock=part_stock)
            s.add(ArticlePart(article_id=article.id, part_id=part.id, qty_per_unit=qty))
        s.flush()
        dispatch = make_dispatch(s, article.id, status="queued", units=units)
        assert tracking.apply_outcome(s, dispatch.id, "completed", "completed")
        inspection_id = s.scalar(select(Inspection.id).where(Inspection.dispatch_id == dispatch.id))
        return article.id, inspection_id


def forms_of(client, action):
    """(attributes, inner html) of every form on the page whose action is `action`."""
    page = client.get(PAGE).text
    return [(a, inner) for a, inner in _FORM.findall(page) if f'action="{action}"' in a]


def token_of(client, action, *, all_form=False):
    """The one-time token of the form `action`; with `all_form` the one of the "all good" button."""
    for _attrs, inner in forms_of(client, action):
        if ('name="all"' in inner) == all_form:
            return _TOKEN.search(inner).group(1)
    raise AssertionError(f"no form {action} (all={all_form})")


def confirm(client, inspection_id, **fields):
    data = {"csrf_token": csrf_of(client, PAGE), "form_token": token_of(client, f"/inspection/{inspection_id}")}
    data.update(fields)
    return client.post(f"/inspection/{inspection_id}", data=data), data


def all_good(client, ids=(), *, everything=False, token=None):
    data = {"csrf_token": csrf_of(client, PAGE),
            "form_token": token or token_of(client, "/inspection/all-good", all_form=everything)}
    if everything:
        data["all"] = "1"
    data["ids"] = [str(i) for i in ids]
    return client.post("/inspection/all-good", data=data), data


def count(client, model, ident, field):
    with sm_of(client).begin() as s:
        return getattr(s.get(model, ident), field)


def open_count(client):
    with sm_of(client).begin() as s:
        return len(s.scalars(select(Inspection).where(Inspection.resolved_at.is_(None))).all())


def test_inspection_empty(ready_client):
    page = ready_client.get(PAGE)
    assert page.status_code == 200
    assert en("inspections.empty") in page.text
    assert "/inspection/all-good" not in page.text  # nothing to confirm, no buttons


def test_nav_inspection_active(ready_client):
    page = ready_client.get(PAGE).text
    link = re.search(r'<a[^>]*href="/inspection"[^>]*>', page)
    assert link, "no nav link to /inspection"
    assert "active" in link.group(0) or "aria-current" in link.group(0)


def lines(page):
    """The html of every row of the list (one per print)."""
    return re.findall(r'<form[^>]*class="[^"]*inspect-line[^"]*"[^>]*>.*?</form>', page, re.S)


def test_list_shows_one_line_per_print(ready_client):  # Spec 8.2 (07.11.2026)
    finished(ready_client, "Template A", units=3, needs_assembly=True)
    finished(ready_client, "Template B")
    page = ready_client.get(PAGE).text
    rows = lines(page)
    assert len(rows) == 2 and "<table" not in page
    first = rows[0]
    assert "Template A" in first and "Template B" in rows[1]
    # the target number (pieces per print), the scrap field starting at 0, the button
    assert re.search(r'<span class="inspect-soll[^"]*">3</span>', first)
    scrap = re.search(r'<input[^>]*name="scrap"[^>]*>', first).group(0)
    assert 'value="0"' in scrap and 'min="0"' in scrap and 'max="3"' in scrap
    assert 'inputmode="numeric"' in scrap and "required" not in scrap
    assert 'name="good"' not in page and 'name="note"' not in page  # no good field, no note
    assert en("inspections.confirm") in first
    assert re.search(r'class="line-head[ "]', page)  # the small column heads
    assert page.count(en("inspections.goes_to_assembly")) == 1
    assert en("inspections.subtitle") in page
    assert "<h1>" in page and en("nav.inspection") in page


def test_list_shows_summary_and_thumbnail(ready_client):
    finished(ready_client, "Template A", units=3, thumbnail=b"png")
    finished(ready_client, "Template B", units=2)
    page = ready_client.get(PAGE).text
    cards = re.findall(r'<span class="kpi-value">(\d+)</span><span class="kpi-label">([^<]*)</span>', page)
    assert cards == [("2", en("kpi.inspections_open")), ("5", en("kpi.inspections_units"))]
    assert 'data-live-region="summary"' in page and 'data-live-topics="inspection"' in page
    assert 'data-live-region="list"' in page and 'data-live-topics="inspection article"' in page
    assert page.count("/thumbnail") == 1


def test_confirm_good(ready_client):  # Spec 12 step 4
    a, i = finished(ready_client)
    ticks = []
    ready_client.app.state.ticker.request_tick = lambda: ticks.append(1)
    response, _ = confirm(ready_client, i, scrap="0")
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert count(ready_client, Article, a, "stock") == 1
    assert count(ready_client, Article, a, "provisional") == 0
    assert rules.available(count(ready_client, Article, a, "stock"), count(ready_client, Article, a, "reserved")) == 1
    assert count(ready_client, Inspection, i, "good_units") == 1
    assert len(ticks) == 1
    page = ready_client.get(PAGE).text
    assert en("inspections.done", name="Template A", good=1, scrap=0) in page
    assert en("inspections.empty") in page
    assert en("inspections.replacement_created") not in page


def test_confirm_scrap_creates_replacement(ready_client):  # Spec 12 step 6
    with sm_of(ready_client).begin() as s:
        other = make_article(s, name="Template Z").id
        make_entry(s, other, 1)
    a, i = finished(ready_client, units=2, min_stock=4)  # p411: a minimum stock, so the scrap leaves the target short
    response, _ = confirm(ready_client, i, scrap="2")
    assert response.status_code == 303
    assert count(ready_client, Article, a, "stock") == 0
    page = ready_client.get(PAGE).text
    assert en("inspections.done", name="Template A", good=0, scrap=2) in page
    assert en("inspections.replacement_created") in page
    queue = ready_client.get("/queue").text
    assert en("queue.origin.scrap") in queue
    assert queue.index("Template A") < queue.index("Template Z")  # at the front


def test_confirm_double_submit(ready_client):  # Review Focus 1
    a, i = finished(ready_client, units=2)
    response, data = confirm(ready_client, i, scrap="0")
    again = ready_client.post(f"/inspection/{i}", data=data)
    assert response.status_code == 303 and again.status_code == 303
    assert count(ready_client, Article, a, "stock") == 2
    with sm_of(ready_client).begin() as s:
        kinds = [e.kind for e in s.scalars(select(JournalEntry))]
    assert kinds.count("inspection_good") == 1
    assert en("form.duplicate") in ready_client.get(PAGE).text


@pytest.mark.parametrize("scrap", ["abc", "-1", "2", "9" * 20, "²"])
def test_confirm_invalid_numbers(ready_client, scrap):  # Review Focus 3
    a, i = finished(ready_client, units=1)
    response, _ = confirm(ready_client, i, scrap=scrap)
    assert response.status_code == 200
    assert 'class="alert"' in response.text
    # One text for every wrong value (p93): from 0 to the target number of the print, never a second range.
    expected = en("form.invalid_number", field=en("inspections.field.scrap"), min=0, max=1)
    assert html.escape(expected) in response.text
    assert "1000" not in response.text
    assert f'name="scrap"' in response.text and f'value="{html.escape(scrap)}"' in response.text  # typed value stays
    assert count(ready_client, Article, a, "stock") == 0
    assert count(ready_client, Article, a, "provisional") == 1
    assert open_count(ready_client) == 1


def test_a_note_in_the_form_is_not_kept(ready_client):  # Spec 6.2: inspection books without a note (p93)
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i, scrap="1", note="should not be saved")
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        entries = list(s.scalars(select(JournalEntry).where(JournalEntry.kind.like("inspection_%"))))
    assert entries and all(e.note is None for e in entries)


def test_all_good_keeps_no_note_either(ready_client):
    a, i = finished(ready_client, units=2)
    response, data = all_good(ready_client, [i])
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert [e.note for e in s.scalars(select(JournalEntry).where(JournalEntry.kind == "inspection_good"))] == [None]


def test_empty_scrap_is_the_default_zero(ready_client):
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i, scrap="")
    assert response.status_code == 303
    assert count(ready_client, Article, a, "stock") == 2


def test_confirm_resolved_elsewhere(ready_client):
    a, i = finished(ready_client)
    forms = forms_of(ready_client, f"/inspection/{i}")
    assert forms
    token = _TOKEN.search(forms[0][1]).group(1)
    with sm_of(ready_client).begin() as s:
        inspection.resolve(s, i, 1, "user")
    data = {"csrf_token": csrf_of(ready_client, PAGE), "form_token": token, "scrap": "0"}
    response = ready_client.post(f"/inspection/{i}", data=data)
    assert response.status_code == 200
    assert en("inspections.already_resolved") in response.text
    assert count(ready_client, Article, a, "stock") == 1


def test_all_good_selected(ready_client):
    ids = [finished(ready_client, f"Template {c}")[1] for c in "ABC"]
    response, _ = all_good(ready_client, ids[:2])
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert open_count(ready_client) == 1
    page = ready_client.get(PAGE).text
    assert en("inspections.all_done", resolved=2, skipped=0) in page
    assert "Template C" in page and len(lines(page)) == 1


def test_all_good_everything(ready_client):
    for c in "ABC":
        finished(ready_client, f"Template {c}")
    ids = re.findall(r'name="ids" value="(\d+)"', "".join(inner for _a, inner in forms_of(ready_client, "/inspection/all-good")))
    assert len(ids) == 3
    response, _ = all_good(ready_client, ids, everything=True)
    assert response.status_code == 303
    assert open_count(ready_client) == 0
    assert en("inspections.all_done", resolved=3, skipped=0) in ready_client.get(PAGE).text


def test_all_good_double_submit(ready_client):
    ids = [finished(ready_client, f"Template {c}")[1] for c in "AB"]
    first, data = all_good(ready_client, ids)
    again = ready_client.post("/inspection/all-good", data=data)
    assert first.status_code == 303 and again.status_code == 303
    with sm_of(ready_client).begin() as s:
        kinds = [e.kind for e in s.scalars(select(JournalEntry))]
    assert kinds.count("inspection_good") == 2
    assert en("form.duplicate") in ready_client.get(PAGE).text


def test_all_good_skips_resolved_elsewhere(ready_client):  # Review Focus 2
    ids = [finished(ready_client, f"Template {c}")[1] for c in "ABC"]
    token = token_of(ready_client, "/inspection/all-good")
    with sm_of(ready_client).begin() as s:
        inspection.resolve(s, ids[0], 1, "user")
    response, _ = all_good(ready_client, ids, token=token)
    assert response.status_code == 303
    assert en("inspections.all_done", resolved=2, skipped=1) in ready_client.get(PAGE).text


@pytest.mark.parametrize("ids", [[], ["abc"], ["0x1"], ["9" * 30]])
def test_all_good_none_selected(ready_client, ids):
    finished(ready_client)
    response, _ = all_good(ready_client, ids)
    assert response.status_code == 200
    assert en("inspections.none_selected") in response.text
    assert open_count(ready_client) == 1


def test_short_parts_flash(ready_client):  # Spec 7.5
    _a, i = finished(ready_client, parts=(("Insert M4", 2, 1),), units=1)
    response, _ = confirm(ready_client, i, scrap="0")
    assert response.status_code == 303
    assert en("stock.parts_short", part="Insert M4", had=1, needed=2) in ready_client.get(PAGE).text


def test_inspection_german(ready_client):
    finished(ready_client, units=2)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get(PAGE).text
    for text in ("Zu prüfen", "Bestätigen", "Alle gut", "Ausschuss", "Soll", de("inspections.subtitle")):
        assert text in html.unescape(page), text


def test_mobile_markup(ready_client):
    finished(ready_client)
    page = ready_client.get(PAGE).text
    assert 'inputmode="numeric"' in page
    assert "<table" not in page
    assert 'class="info"' in page and en("info.inspection_scrap") in html.unescape(page)
    assert re.search(r'<label class="inspect-select">', page)  # a tick box, not a switch (p117)


# ---- the filter by article (Spec 8.2; the target of the later QR code) ------------------------


def two_articles(client):
    a, ia = finished(client, "Template A", units=2)
    b, ib = finished(client, "Template B", units=3)
    return a, ia, b, ib


def test_filter_shows_only_the_prints_of_the_article_with_a_chip(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    page = ready_client.get(f"{PAGE}?article={a}").text
    rows = lines(page)
    assert len(rows) == 1 and "Template A" in rows[0] and "Template B" not in "".join(rows)
    assert f'action="/inspection/{ia}"' in page and f'action="/inspection/{ib}"' not in page
    chip = re.search(r'<a class="chip"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', page, re.S)
    assert chip and chip.group(1) == PAGE  # taking it off goes back to the whole list
    assert "Template A" in chip.group(2) and en("list.filter_article") in chip.group(2)
    # the key figures follow the filter
    cards = re.findall(r'<span class="kpi-value">(\d+)</span>', page)
    assert cards == ["1", "2"]


@pytest.mark.parametrize("raw", ["abc", "", "0x1", "999999", "-1", "9" * 30])
def test_a_bad_or_unknown_article_filter_is_no_filter(ready_client, raw):
    two_articles(ready_client)
    page = ready_client.get(PAGE, params={"article": raw}).text
    assert len(lines(page)) == 2 and 'class="chip"' not in page


def test_filtered_list_without_prints_of_that_article(ready_client):
    a, *_ = two_articles(ready_client)
    with sm_of(ready_client).begin() as s:
        other = make_article(s, name="Template C").id
    page = ready_client.get(f"{PAGE}?article={other}").text
    assert lines(page) == [] and en("inspections.empty_article") in page
    assert 'class="chip"' in page


def test_confirming_in_the_filtered_list_returns_to_it(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    forms = [(at, inner) for at, inner in _FORM.findall(ready_client.get(f"{PAGE}?article={a}").text)
             if f'action="/inspection/{ia}"' in at]
    inner = forms[0][1]
    assert 'name="article" value="%d"' % a in inner
    data = {"csrf_token": csrf_of(ready_client, PAGE), "form_token": _TOKEN.search(inner).group(1),
            "scrap": "0", "article": str(a)}
    response = ready_client.post(f"/inspection/{ia}", data=data)
    assert response.status_code == 303 and response.headers["location"] == f"{PAGE}?article={a}"
    assert open_count(ready_client) == 1  # B is still waiting


def test_a_failed_confirm_in_the_filtered_list_keeps_the_filter(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    data = {"csrf_token": csrf_of(ready_client, PAGE),
            "form_token": token_of(ready_client, f"/inspection/{ia}"), "scrap": "99", "article": str(a)}
    response = ready_client.post(f"/inspection/{ia}", data=data)
    assert response.status_code == 200
    assert len(lines(response.text)) == 1 and 'class="chip"' in response.text


def test_all_good_of_the_filtered_list_touches_only_that_article(ready_client):
    """The server filters by article itself: a form that carries ids of other articles (a prints
    that came in after the page was drawn, or a hand-made request) does not touch them."""
    a, ia, b, ib = two_articles(ready_client)
    _c, ic = finished(ready_client, "Template A", units=1)  # a second print of A, came later
    forms = forms_of(ready_client, "/inspection/all-good")
    token = next(_TOKEN.search(inner).group(1) for at, inner in forms if 'name="all"' in inner)
    data = {"csrf_token": csrf_of(ready_client, PAGE), "form_token": token, "all": "1",
            "article": str(a), "ids": [str(ia), str(ib)]}  # B's print is in the request
    response = ready_client.post("/inspection/all-good", data=data)
    assert response.status_code == 303 and response.headers["location"] == f"{PAGE}?article={a}"
    assert count(ready_client, Inspection, ia, "resolved_at") is not None
    assert count(ready_client, Inspection, ib, "resolved_at") is None  # B stays open
    assert count(ready_client, Article, b, "provisional") == 3 and count(ready_client, Article, b, "stock") == 0
    assert count(ready_client, Inspection, ic, "resolved_at") is None  # not in the request, not touched
    page = ready_client.get(f"{PAGE}?article={a}").text
    assert en("inspections.all_done", resolved=1, skipped=0) in page


def test_the_all_good_form_of_the_filtered_list_carries_only_its_prints(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    inner = "".join(i for _a, i in forms_of_page(ready_client.get(f"{PAGE}?article={a}").text, "/inspection/all-good"))
    assert re.findall(r'name="ids" value="(\d+)"', inner) == [str(ia)]
    assert 'name="article" value="%d"' % a in inner


def forms_of_page(page, action):
    return [(a, inner) for a, inner in _FORM.findall(page) if f'action="{action}"' in a]


def test_a_form_of_the_old_page_with_a_good_field_is_refused_not_misread(ready_client):
    """A tab opened before the update still posts `good`; reading only `scrap` (default 0) would
    book every unit as good and lose the scrap. The server refuses and books nothing."""
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i, good="0")
    assert response.status_code == 200
    assert html.escape(en("inspections.form_outdated")) in response.text
    assert count(ready_client, Article, a, "stock") == 0
    assert count(ready_client, Article, a, "provisional") == 2
    assert open_count(ready_client) == 1


def test_p411_scrap_with_the_target_reached_says_no_replacement_is_needed(ready_client):
    """Spec 7.5 (0.4.5): a print of the minimum stock is replaced only up to the target."""
    a, i = finished(ready_client, units=2, stock=9, min_stock=4, buffer=2)
    response, _ = confirm(ready_client, i, scrap="2")
    assert response.status_code == 303
    page = ready_client.get(PAGE).text
    assert en("inspections.replacement_not_needed") in page
    assert en("inspections.replacement_created") not in page
    assert en("queue.origin.scrap") not in ready_client.get("/queue").text
