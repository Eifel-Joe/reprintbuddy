"""Page "To check" (Spec 8.2, 10.3, 12): finished prints waiting for the count of good pieces."""

import html
import re

import pytest
from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_article, make_dispatch, make_entry, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import inspection, rules, tracking
from reprintbuddy.models import Article, ArticlePart, Inspection, JournalEntry
from reprintbuddy.web.i18n import translate

PAGE = "/inspection"
_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def en(key, **params):
    return translate("en", key, **params)


def de(key, **params):
    return translate("de", key, **params)


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


def test_list_shows_cards(ready_client):  # Spec 8.2
    finished(ready_client, "Template A", needs_assembly=True)
    finished(ready_client, "Template B")
    page = ready_client.get(PAGE).text
    assert page.count('class="card section inspect-card"') == 2
    assert "Template A" in page and "Template B" in page
    assert len(re.findall(r'name="good"[^>]*value="1"', page)) == 2  # the number field starts at the units
    assert page.count(en("inspections.goes_to_assembly")) == 1
    assert en("inspections.confirm") in page
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
    response, _ = confirm(ready_client, i, good="1")
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
    a, i = finished(ready_client, units=2)
    response, _ = confirm(ready_client, i, good="0")
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
    response, data = confirm(ready_client, i, good="2")
    again = ready_client.post(f"/inspection/{i}", data=data)
    assert response.status_code == 303 and again.status_code == 303
    assert count(ready_client, Article, a, "stock") == 2
    with sm_of(ready_client).begin() as s:
        kinds = [e.kind for e in s.scalars(select(JournalEntry))]
    assert kinds.count("inspection_good") == 1
    assert en("form.duplicate") in ready_client.get(PAGE).text


@pytest.mark.parametrize("good", ["", "abc", "-1", "2", "9" * 20, "²"])
def test_confirm_invalid_numbers(ready_client, good):  # Review Focus 3
    a, i = finished(ready_client, units=1)
    response, _ = confirm(ready_client, i, good=good)
    assert response.status_code == 200
    assert 'class="alert"' in response.text
    field = en("inspections.field.good")
    if good == "":
        expected = en("form.required", field=field)
    elif good == "2":
        expected = en("form.invalid_number", field=field, min=0, max=1)
    else:
        expected = en("form.invalid_number", field=field, min=0, max=1000)
    assert html.escape(expected) in response.text
    assert count(ready_client, Article, a, "stock") == 0
    assert count(ready_client, Article, a, "provisional") == 1
    assert open_count(ready_client) == 1


def test_confirm_resolved_elsewhere(ready_client):
    a, i = finished(ready_client)
    forms = forms_of(ready_client, f"/inspection/{i}")
    assert forms
    token = _TOKEN.search(forms[0][1]).group(1)
    with sm_of(ready_client).begin() as s:
        inspection.resolve(s, i, 1, "user")
    data = {"csrf_token": csrf_of(ready_client, PAGE), "form_token": token, "good": "1"}
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
    assert "Template C" in page and page.count('class="card section inspect-card"') == 1


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
    response, _ = confirm(ready_client, i, good="1")
    assert response.status_code == 303
    assert en("stock.parts_short", part="Insert M4", had=1, needed=2) in ready_client.get(PAGE).text


def test_inspection_german(ready_client):
    finished(ready_client, units=2)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get(PAGE).text
    for text in ("Zu prüfen", "Bestätigen", "Alle gut", "Gute Einheiten", de("inspections.subtitle")):
        assert text in html.unescape(page), text
    assert "Ausschuss" in html.unescape(page)  # in the explanation of the field


def test_mobile_markup(ready_client):
    finished(ready_client)
    page = ready_client.get(PAGE).text
    assert 'inputmode="numeric"' in page
    assert "<table" not in page
    assert 'class="info"' in page and en("info.inspection_good") in html.unescape(page)
    assert re.search(r'<label class="check[ "]', page)
