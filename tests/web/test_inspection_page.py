"""Page "To check" (Spec 8.2, 10.3, 12): finished prints waiting for the count of good pieces."""

import html
import re
from datetime import datetime

import pytest
from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_article, make_dispatch, make_entry, make_part
from texts import de, en, en_html
from reprintbuddy import settings as st
from reprintbuddy.domain import inspection, rules, tracking
from reprintbuddy.models import Article, ArticlePart, Inspection, JournalEntry

PAGE = "/inspection"
_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def finished(client, name="Template A", units=1, *, parts=(), origin="rule", **fields):
    """An article with a finished print that waits for inspection; returns (article id, inspection id).

    A second call with the same name adds a print to the same article. `parts` are (name,
    qty_per_unit, stock) of bought parts built into the article; `origin` is that of the print.
    """
    with sm_of(client).begin() as s:
        article = s.scalar(select(Article).where(Article.name == name)) or make_article(s, name=name, **fields)
        for part_name, qty, part_stock in parts:
            part = make_part(s, name=part_name, stock=part_stock)
            s.add(ArticlePart(article_id=article.id, part_id=part.id, qty_per_unit=qty))
        s.flush()
        dispatch = make_dispatch(s, article.id, status="queued", units=units, origin=origin)
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


def any_token(client, url=PAGE):
    """A one-time token from the page (a token is not bound to one form)."""
    return _TOKEN.search(client.get(url).text).group(1)


def confirm(client, inspection_id, **fields):
    """The form of one print: unfolded under its article, or (one print) a request of an older page."""
    forms = forms_of(client, f"/inspection/{inspection_id}")
    token = _TOKEN.search(forms[0][1]).group(1) if forms else any_token(client)
    data = {"csrf_token": csrf_of(client, PAGE), "form_token": token}
    data.update(fields)
    return client.post(f"/inspection/{inspection_id}", data=data), data


def row_form(client, article_id, url=PAGE):
    """(inner html, ids) of the row of `article_id` on the page at `url`."""
    page = client.get(url).text
    inner = next(i for a, i in _FORM.findall(page) if f'action="/inspection/articles/{article_id}"' in a)
    return inner, re.findall(r'type="hidden" name="ids" value="(\d+)"', inner)


def confirm_row(client, article_id, good, *, ids=None, token=None, url=PAGE, **fields):
    """Confirm the row of an article with `good` pieces, as the page sends it."""
    inner, shown = row_form(client, article_id, url)
    data = {"csrf_token": csrf_of(client, PAGE), "form_token": token or _TOKEN.search(inner).group(1),
            "ids": ids if ids is not None else shown, "good": good}
    data.update(fields)
    return client.post(f"/inspection/articles/{article_id}", data=data), data


def all_good(client, ids=(), *, everything=False, token=None, groups=()):
    data = {"csrf_token": csrf_of(client, PAGE),
            "form_token": token or token_of(client, "/inspection/all-good", all_form=everything)}
    if everything:
        data["all"] = "1"
    data["ids"] = [str(i) for i in ids]
    data["group"] = [" ".join(str(i) for i in g) for g in groups]
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
    """The html of every row of the list (one per article, Spec 8.2 since 20.11.2026)."""
    return re.findall(r'<form[^>]*class="[^"]*inspect-line[^"]*"[^>]*>.*?</form>', page, re.S)


def print_lines(page):
    """The html of every unfolded print (the forms of single prints under an article)."""
    return re.findall(r'<form[^>]*class="[^"]*inspect-print[^"]*"[^>]*>.*?</form>', page, re.S)


def unfold(page, article_id):
    """The <details> with the single prints of `article_id`, or None."""
    m = re.search(r'<details class="inspect-prints"[^>]*data-article="%d"[^>]*>.*?</details>' % article_id, page, re.S)
    return m.group(0) if m else None


def test_list_shows_one_line_per_article(ready_client):  # Spec 8.2 (20.11.2026, p713)
    a, i1 = finished(ready_client, "Template A", units=3, needs_assembly=True)
    _a, i2 = finished(ready_client, "Template A", units=3)
    finished(ready_client, "Template B")
    page = ready_client.get(PAGE).text
    rows = lines(page)
    assert len(rows) == 2 and "<table" not in page
    first = rows[0]
    assert "Template A" in first and "Template B" in rows[1]
    # the target number of the row (units per print x prints), the field "good" starting at it, the button
    assert re.search(r'<span class="inspect-soll[^"]*">6</span>', first)
    good = re.search(r'<input[^>]*name="good"[^>]*>', first).group(0)
    assert 'value="6"' in good and 'min="0"' in good and 'max="6"' in good
    assert 'inputmode="numeric"' in good and "required" in good  # p713 F1: empty is no answer
    assert 'name="scrap"' not in page and 'name="note"' not in page  # no scrap field, no note
    assert re.findall(r'type="hidden" name="ids" value="(\d+)"', first) == [str(i1), str(i2)]
    assert en("inspections.confirm") in first
    assert re.search(r'class="line-head[ "]', page)  # the small column heads
    assert page.count(en("inspections.goes_to_assembly")) == 1
    assert en("inspections.subtitle") in page
    assert "<h1>" in page and en("nav.inspection") in page


def test_the_row_names_the_number_of_prints_and_every_finish_time(ready_client):  # Spec 8.2
    a, i1 = finished(ready_client, "Template A")
    _a, i2 = finished(ready_client, "Template A")
    with sm_of(ready_client).begin() as s:
        s.get(Inspection, i1).created_at = datetime(2026, 3, 2, 8, 5)
        s.get(Inspection, i2).created_at = datetime(2026, 3, 2, 9, 40)
    first = html.unescape(lines(ready_client.get(PAGE).text)[0])
    meta = re.search(r'<div class="line-meta">(.*?)</div>', first, re.S).group(1)
    times = re.findall(r"\d{1,2}:\d{2}", meta)
    assert len(times) == 2 and times[0] != times[1]  # one time per print, in the browser's zone
    head = en("inspections.prints_finished", n=2, at="@@").split("@@")[0]
    assert head in meta


def test_a_single_print_has_no_unfold(ready_client):  # p713 F2
    a, _i = finished(ready_client, "Template A", units=2)
    page = ready_client.get(PAGE).text
    assert unfold(page, a) is None and print_lines(page) == []
    assert en("inspections.finished_at", at="@@").split("@@")[0] in html.unescape(lines(page)[0])


def test_several_prints_unfold_to_one_form_each(ready_client):  # Spec 8.2: own time, own field, own button
    a, i1 = finished(ready_client, "Template A", units=2)
    _a, i2 = finished(ready_client, "Template A", units=3)
    page = ready_client.get(PAGE).text
    details = unfold(page, a)
    assert details and " open" not in details.split(">")[0]
    assert en("inspections.unfold", n=2) in html.unescape(details)
    prints = print_lines(details)
    assert len(prints) == 2
    assert f'action="/inspection/{i1}"' in prints[0] and f'action="/inspection/{i2}"' in prints[1]
    good = re.search(r'<input[^>]*name="good"[^>]*>', prints[1]).group(0)
    assert 'value="3"' in good and 'max="3"' in good and "required" in good
    assert en("inspections.confirm") in prints[0]
    assert en("inspections.finished_at", at="@@").split("@@")[0] in html.unescape(prints[0])


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
    response, _ = confirm_row(ready_client, a, "1")
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert count(ready_client, Article, a, "stock") == 1
    assert count(ready_client, Article, a, "provisional") == 0
    assert rules.available(count(ready_client, Article, a, "stock"), count(ready_client, Article, a, "reserved")) == 1
    assert count(ready_client, Inspection, i, "good_units") == 1
    assert len(ticks) == 1
    page = ready_client.get(PAGE).text
    assert en("inspections.done", name="Template A", good=1, scrap=0) in page
    assert en("inspections.empty") in page
    assert en_html("inspections.replacement_created") not in page


def test_confirm_article_row_of_several_prints(ready_client):  # Spec 8.2: scrap = target - good
    with sm_of(ready_client).begin() as s:
        other = make_article(s, name="Template Z").id
        make_entry(s, other, 1)
    a, i1 = finished(ready_client, units=4, origin="manual")
    _a, i2 = finished(ready_client, units=4, origin="manual")
    response, _ = confirm_row(ready_client, a, "5")
    assert response.status_code == 303
    assert count(ready_client, Article, a, "stock") == 5
    assert count(ready_client, Inspection, i1, "good_units") == 1  # the oldest print takes the scrap
    assert count(ready_client, Inspection, i2, "good_units") == 4
    page = ready_client.get(PAGE).text
    assert en("inspections.group_done", name="Template A", good=5, scrap=3, prints=2) in page
    assert en_html("inspections.replacement_created") in page
    queue = ready_client.get("/queue").text
    assert queue.index("Template A") < queue.index("Template Z")  # at the front


def test_several_replacements_are_named_with_their_number(ready_client):
    a, _ = finished(ready_client, units=2, origin="manual")
    finished(ready_client, units=2, origin="manual")
    finished(ready_client, units=2, origin="manual")
    response, _ = confirm_row(ready_client, a, "1")  # scrap 5: 2 + 2 + 1, one replacement per print
    assert response.status_code == 303
    assert en_html("inspections.replacements_created", n=3) in ready_client.get(PAGE).text


def test_confirm_scrap_creates_replacement(ready_client):  # Spec 12 step 6
    with sm_of(ready_client).begin() as s:
        other = make_article(s, name="Template Z").id
        make_entry(s, other, 1)
    a, i = finished(ready_client, units=2, min_stock=4)  # p411: a minimum stock, so the scrap leaves the target short
    response, _ = confirm_row(ready_client, a, "0")
    assert response.status_code == 303
    assert count(ready_client, Article, a, "stock") == 0
    page = ready_client.get(PAGE).text
    assert en("inspections.done", name="Template A", good=0, scrap=2) in page
    assert en_html("inspections.replacement_created") in page
    queue = ready_client.get("/queue").text
    assert en("queue.origin.scrap") in queue
    assert queue.index("Template A") < queue.index("Template Z")  # at the front


def test_confirm_double_submit(ready_client):  # Review Focus 1
    a, i = finished(ready_client, units=2)
    response, data = confirm_row(ready_client, a, "2")
    again = ready_client.post(f"/inspection/articles/{a}", data=data)
    assert response.status_code == 303 and again.status_code == 303
    assert count(ready_client, Article, a, "stock") == 2
    with sm_of(ready_client).begin() as s:
        kinds = [e.kind for e in s.scalars(select(JournalEntry))]
    assert kinds.count("inspection_good") == 1
    assert en("form.duplicate") in ready_client.get(PAGE).text


@pytest.mark.parametrize("good", ["", "abc", "-1", "5", "2,5", "9" * 20, "²"])
def test_confirm_row_invalid_numbers(ready_client, good):  # Review Focus 3, p713 F1
    a, _ = finished(ready_client, units=2)
    finished(ready_client, units=2)
    response, _ = confirm_row(ready_client, a, good)
    assert response.status_code == 200
    # One text for every wrong value (p93): from 0 to the target number of the row.
    expected = en("form.invalid_number", field=en("inspections.field.good"), min=0, max=4)
    assert html.escape(expected) in response.text
    row = lines(response.text)[0]
    assert f'value="{html.escape(good)}"' in row  # the typed value stays
    assert count(ready_client, Article, a, "stock") == 0
    assert count(ready_client, Article, a, "provisional") == 4
    assert open_count(ready_client) == 2


def test_a_row_changed_elsewhere_books_nothing(ready_client):  # Review Focus 2, p713 F3
    a, i1 = finished(ready_client, units=2)
    _a, i2 = finished(ready_client, units=2)
    inner, ids = row_form(ready_client, a)
    token = _TOKEN.search(inner).group(1)
    with sm_of(ready_client).begin() as s:
        inspection.resolve(s, i2, 2, "user")
    response, _ = confirm_row(ready_client, a, "3", ids=ids, token=token)
    assert response.status_code == 200
    assert html.escape(en("inspections.group_changed")) in response.text
    assert count(ready_client, Inspection, i1, "resolved_at") is None
    assert count(ready_client, Article, a, "stock") == 2  # only the one confirmed elsewhere


def test_a_print_reported_after_the_page_stays_open(ready_client):  # Review Focus 1
    a, i1 = finished(ready_client, units=2)
    inner, ids = row_form(ready_client, a)
    _a, later = finished(ready_client, units=2)
    response, _ = confirm_row(ready_client, a, "2", ids=ids, token=_TOKEN.search(inner).group(1))
    assert response.status_code == 303
    assert count(ready_client, Inspection, i1, "resolved_at") is not None
    assert count(ready_client, Inspection, later, "resolved_at") is None


def test_a_note_in_the_form_is_not_kept(ready_client):  # Spec 6.2: inspection books without a note (p93)
    a, i = finished(ready_client, units=2)
    response, _ = confirm_row(ready_client, a, "1", note="should not be saved")
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


# ---- a single print, unfolded ------------------------------------------------------------------------


def test_single_print_confirm_by_good(ready_client):
    a, i1 = finished(ready_client, units=3)
    _a, i2 = finished(ready_client, units=3)
    response, _ = confirm(ready_client, i2, good="2")
    assert response.status_code == 303
    assert count(ready_client, Inspection, i2, "good_units") == 2
    assert count(ready_client, Inspection, i1, "resolved_at") is None
    page = ready_client.get(PAGE).text
    assert en("inspections.done", name="Template A", good=2, scrap=1) in page
    # the confirmed print leaves the row: one print left, nothing to unfold
    assert re.search(r'<span class="inspect-soll[^"]*">3</span>', lines(page)[0]) and unfold(page, a) is None


@pytest.mark.parametrize("good", ["", "abc", "4"])
def test_error_on_single_print_opens_its_row(ready_client, good):
    a, i1 = finished(ready_client, units=3)
    _a, i2 = finished(ready_client, units=3)
    response, _ = confirm(ready_client, i2, good=good)
    assert response.status_code == 200
    details = unfold(response.text, a)
    assert details and " open" in details.split(">")[0]
    expected = en("form.invalid_number", field=en("inspections.field.good"), min=0, max=3)
    assert html.escape(expected) in details
    assert open_count(ready_client) == 2


def test_old_page_single_print_by_scrap_still_books_right(ready_client):  # Review Focus 5, p713 F7
    """A tab opened before the update posts `scrap` to the print: that is unambiguous, so it books."""
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i, scrap="1")
    assert response.status_code == 303
    assert count(ready_client, Inspection, i, "good_units") == 1
    assert count(ready_client, Article, a, "stock") == 1


def test_old_page_empty_scrap_is_still_zero(ready_client):
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i, scrap="")
    assert response.status_code == 303
    assert count(ready_client, Article, a, "stock") == 2


def test_a_print_form_without_any_count_is_refused(ready_client):
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i)
    assert response.status_code == 200
    assert count(ready_client, Article, a, "provisional") == 2


def test_confirm_resolved_elsewhere(ready_client):
    a, i = finished(ready_client)
    token = any_token(ready_client)
    with sm_of(ready_client).begin() as s:
        inspection.resolve(s, i, 1, "user")
    data = {"csrf_token": csrf_of(ready_client, PAGE), "form_token": token, "good": "1"}
    response = ready_client.post(f"/inspection/{i}", data=data)
    assert response.status_code == 200
    assert en("inspections.already_resolved") in response.text
    assert count(ready_client, Article, a, "stock") == 1


# ---- all good, selected all good (p713: per article) ----------------------------------------------------


def test_the_select_box_of_a_row_carries_all_its_prints(ready_client):
    a, i1 = finished(ready_client, "Template A")
    _a, i2 = finished(ready_client, "Template A")
    page = ready_client.get(PAGE).text
    box = re.search(r'<input type="checkbox" name="group" value="([^"]*)" form="inspect-selected"[^>]*>', lines(page)[0])
    assert box and box.group(1) == f"{i1} {i2}"
    assert html.escape(en("inspections.select_article", name="Template A")) in box.group(0)


def test_all_good_selected_by_article(ready_client):
    ids = {c: [finished(ready_client, f"Template {c}")[1] for _ in range(2)] for c in "ABC"}
    response, _ = all_good(ready_client, groups=[ids["A"], ids["B"]])
    assert response.status_code == 303 and response.headers["location"] == PAGE
    assert open_count(ready_client) == 2
    page = ready_client.get(PAGE).text
    assert en("inspections.all_done", resolved=4, skipped=0) in page
    assert "Template C" in page and len(lines(page)) == 1


def test_old_page_all_good_with_ids_still_works(ready_client):  # Review Focus 5, p713 F7
    ids = [finished(ready_client, f"Template {c}")[1] for c in "ABC"]
    response, _ = all_good(ready_client, ids[:2])
    assert response.status_code == 303
    assert open_count(ready_client) == 1


def test_all_good_everything(ready_client):
    for c in "ABC":
        finished(ready_client, f"Template {c}")
    finished(ready_client, "Template A")
    ids = re.findall(r'name="ids" value="(\d+)"', "".join(inner for _a, inner in forms_of(ready_client, "/inspection/all-good")))
    assert len(ids) == 4
    response, _ = all_good(ready_client, ids, everything=True)
    assert response.status_code == 303
    assert open_count(ready_client) == 0
    assert en("inspections.all_done", resolved=4, skipped=0) in ready_client.get(PAGE).text


def test_all_good_double_submit(ready_client):
    ids = [finished(ready_client, f"Template {c}")[1] for c in "AB"]
    first, data = all_good(ready_client, groups=[[i] for i in ids])
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
    response, _ = all_good(ready_client, groups=[[i] for i in ids], token=token)
    assert response.status_code == 303
    assert en("inspections.all_done", resolved=2, skipped=1) in ready_client.get(PAGE).text


@pytest.mark.parametrize("groups", [[], ["abc"], ["0x1"], ["9" * 30], [" "], ["1;2"]])
def test_all_good_none_selected(ready_client, groups):
    finished(ready_client)
    data = {"csrf_token": csrf_of(ready_client, PAGE), "form_token": token_of(ready_client, "/inspection/all-good"),
            "group": groups}
    response = ready_client.post("/inspection/all-good", data=data)
    assert response.status_code == 200
    assert en("inspections.none_selected") in response.text
    assert open_count(ready_client) == 1


def test_short_parts_flash(ready_client):  # Spec 7.5
    a, i = finished(ready_client, parts=(("Insert M4", 2, 1),), units=1)
    finished(ready_client, units=1)
    response, _ = confirm_row(ready_client, a, "2")
    assert response.status_code == 303
    page = ready_client.get(PAGE).text
    assert en("stock.parts_short", part="Insert M4", had=1, needed=4) in page  # one message per part
    assert page.count("Insert M4") == 1


def test_inspection_german(ready_client):
    finished(ready_client, units=2)
    finished(ready_client, units=2)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = html.unescape(ready_client.get(PAGE).text)
    for text in ("Zu prüfen", "Bestätigen", "Alle gut", de("inspections.field.good"), "Soll",
                 de("inspections.subtitle"), de("inspections.unfold", n=2)):
        assert text in page, text


def test_mobile_markup(ready_client):
    a, _ = finished(ready_client)
    finished(ready_client)
    page = ready_client.get(PAGE).text
    assert 'inputmode="numeric"' in page
    assert "<table" not in page
    assert 'class="info"' in page and en("info.inspection_good") in html.unescape(page)
    assert re.search(r'<label class="inspect-select">', page)  # a tick box, not a switch (p117)
    assert "<summary" in unfold(page, a)  # unfolds without a script too


# ---- the filter by article (Spec 8.2; the target of the QR code, 8.4) ------------------------


def two_articles(client):
    a, ia = finished(client, "Template A", units=2)
    b, ib = finished(client, "Template B", units=3)
    return a, ia, b, ib


def test_filter_shows_only_the_prints_of_the_article_with_a_chip(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    page = ready_client.get(f"{PAGE}?article={a}").text
    rows = lines(page)
    assert len(rows) == 1 and "Template A" in rows[0] and "Template B" not in "".join(rows)
    assert f'action="/inspection/articles/{a}"' in page and f'action="/inspection/articles/{b}"' not in page
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
    inner, _ids = row_form(ready_client, a, f"{PAGE}?article={a}")
    assert 'name="article" value="%d"' % a in inner
    response, _ = confirm_row(ready_client, a, "2", url=f"{PAGE}?article={a}", article=str(a))
    assert response.status_code == 303 and response.headers["location"] == f"{PAGE}?article={a}"
    assert open_count(ready_client) == 1  # B is still waiting


def test_a_failed_confirm_in_the_filtered_list_keeps_the_filter(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    response, _ = confirm_row(ready_client, a, "99", url=f"{PAGE}?article={a}", article=str(a))
    assert response.status_code == 200
    assert len(lines(response.text)) == 1 and 'class="chip"' in response.text


def test_a_print_of_another_article_is_not_booked_through_a_row(ready_client):
    a, ia, b, ib = two_articles(ready_client)
    response, _ = confirm_row(ready_client, a, "2", ids=[str(ib)])  # B's print sent to A's row
    assert response.status_code == 200
    assert html.escape(en("inspections.group_changed")) in response.text
    assert count(ready_client, Inspection, ib, "resolved_at") is None


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


def test_p411_scrap_with_the_target_reached_says_no_replacement_is_needed(ready_client):
    """Spec 7.5 (0.4.5): a print of the minimum stock is replaced only up to the target."""
    a, i = finished(ready_client, units=2, stock=9, min_stock=4, buffer=2)
    response, _ = confirm_row(ready_client, a, "0")
    assert response.status_code == 303
    page = ready_client.get(PAGE).text
    assert en("inspections.replacement_not_needed") in page
    assert en_html("inspections.replacement_created") not in page
    assert en("queue.origin.scrap") not in ready_client.get("/queue").text


# ---- review fixes (p713) ------------------------------------------------------------------------------


def test_after_a_changed_row_the_field_shows_the_new_target_not_the_old_count(ready_client):
    """F3: the typed count belonged to the old prints; refilling it would let one tap book it against
    the new ones. The row comes back with its new target, to be counted again."""
    a, i1 = finished(ready_client, units=4)
    _a, i2 = finished(ready_client, units=4)
    inner, ids = row_form(ready_client, a)
    token = _TOKEN.search(inner).group(1)
    with sm_of(ready_client).begin() as s:
        inspection.resolve(s, i2, 4, "user")
    finished(ready_client, units=4)  # a new print came in meanwhile: the row is again 8
    response, _ = confirm_row(ready_client, a, "5", ids=ids, token=token)
    assert response.status_code == 200
    good = re.search(r'<input[^>]*name="good"[^>]*>', lines(response.text)[0]).group(0)
    assert 'value="8"' in good and 'value="5"' not in good


def test_the_all_good_message_counts_prints():
    assert "print" in en("inspections.all_done") and "Druck" in de("inspections.all_done")


def test_the_help_says_a_replacement_comes_only_if_needed():  # p411, Spec 7.5
    assert "if needed" in en("info.inspection_good") and "falls nötig" in de("info.inspection_good")
