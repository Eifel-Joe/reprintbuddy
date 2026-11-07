"""JSON interface of the inspection (Spec 8.2): a small, stable surface for a later shelf display."""

import json
from datetime import datetime

import pytest
from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_article, make_entry
from reprintbuddy.models import Article, Inspection, JournalEntry, WorkQueueEntry
from test_inspection_page import PAGE, confirm, count, en, finished, de, open_count

LIST_URL = "/api/inspections"


def api_post(client, inspection_id, body=None, *, raw=None, token=True, content_type="application/json"):
    """POST to the interface the way a display would: JSON body, token in the header."""
    headers = {"Content-Type": content_type}
    if token:
        headers["X-CSRF-Token"] = csrf_of(client, PAGE)
    content = raw if raw is not None else json.dumps(body)
    return client.post(f"{LIST_URL}/{inspection_id}", content=content, headers=headers)


def journal_kinds(client):
    with sm_of(client).begin() as s:
        return [e.kind for e in s.scalars(select(JournalEntry).order_by(JournalEntry.id))]


def test_api_list(ready_client):
    a1, i1 = finished(ready_client, "Template A", units=2, needs_assembly=True)
    a2, i2 = finished(ready_client, "Template B")
    response = ready_client.get(LIST_URL)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    rows = response.json()["inspections"]
    assert [r["id"] for r in rows] == [i1, i2]
    assert rows[0] == {
        "id": i1, "article_id": a1, "article_name": "Template A", "units": 2,
        "needs_assembly": True, "created_at": rows[0]["created_at"],
    }
    assert rows[1]["needs_assembly"] is False and rows[1]["article_id"] == a2
    for row in rows:
        assert row["created_at"].endswith("+00:00")
        assert datetime.fromisoformat(row["created_at"]).utcoffset().total_seconds() == 0


def test_api_list_empty(ready_client):
    response = ready_client.get(LIST_URL)
    assert response.status_code == 200 and response.json() == {"inspections": []}


def test_api_resolve_good(ready_client):
    a, i = finished(ready_client)
    ticks = []
    ready_client.app.state.ticker.request_tick = lambda: ticks.append(1)  # a write wakes the engine
    response = api_post(ready_client, i, {"good_units": 1})
    assert response.status_code == 200 and len(ticks) == 1
    body = response.json()
    assert body == {"id": i, "good": 1, "scrap": 0, "replacement_entry_id": None, "part_shortages": []}
    assert count(ready_client, Article, a, "stock") == 1
    assert count(ready_client, Article, a, "provisional") == 0
    assert open_count(ready_client) == 0
    assert ready_client.get(LIST_URL).json() == {"inspections": []}


def test_api_resolve_scrap(ready_client):
    with sm_of(ready_client).begin() as s:
        other = make_article(s, name="Template Z").id
        make_entry(s, other, 1)
    a, i = finished(ready_client, units=2)
    response = api_post(ready_client, i, {"good_units": 0})
    assert response.status_code == 200
    body = response.json()
    assert body["good"] == 0 and body["scrap"] == 2
    assert isinstance(body["replacement_entry_id"], int) and not isinstance(body["replacement_entry_id"], bool)
    with sm_of(ready_client).begin() as s:
        entry = s.get(WorkQueueEntry, body["replacement_entry_id"])
        assert entry.article_id == a and entry.origin == "scrap"
        first = s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position, WorkQueueEntry.id)).first()
        assert first.id == entry.id  # at the front
    assert count(ready_client, Article, a, "stock") == 0


def test_api_resolve_reports_part_shortages(ready_client):
    a, i = finished(ready_client, parts=[("Screw", 2, 0)])
    body = api_post(ready_client, i, {"good_units": 1}).json()
    assert len(body["part_shortages"]) == 1
    shortage = body["part_shortages"][0]
    assert set(shortage) == {"part_id", "needed", "had"}
    assert shortage["needed"] == 2 and shortage["had"] == 0


def test_api_resolve_conflict(ready_client):  # Review Focus 2
    a, i = finished(ready_client)
    assert api_post(ready_client, i, {"good_units": 1}).status_code == 200
    again = api_post(ready_client, i, {"good_units": 1})
    assert again.status_code == 409
    body = again.json()
    assert body["error"] == "inspections.already_resolved"
    assert body["message"] == en("inspections.already_resolved")
    assert count(ready_client, Article, a, "stock") == 1
    assert journal_kinds(ready_client).count("inspection_good") == 1


def test_api_message_follows_the_language(ready_client):
    finished(ready_client)
    ready_client.post("/settings/language", data={"csrf_token": csrf_of(ready_client, "/settings"), "language": "de"})
    response = api_post(ready_client, 9999, {"good_units": 1})
    assert response.status_code == 404
    assert response.json() == {"error": "inspections.not_found", "message": de("inspections.not_found")}


@pytest.mark.parametrize(
    "body, raw",
    [
        ({}, None),
        ({"good_units": "1"}, None),
        ({"good_units": True}, None),
        ({"good_units": 1.0}, None),
        ({"good_units": None}, None),
        ({"good_units": -1}, None),
        ({"good_units": 2}, None),  # more than the units of the print
        ({"scrap_units": "0"}, None),
        ({"scrap_units": True}, None),
        ({"scrap_units": 0.0}, None),
        ({"scrap_units": None}, None),
        ({"scrap_units": -1}, None),
        ({"scrap_units": 2}, None),  # more than the units of the print
        ({"scrap_units": 10**30}, None),
        ({"good_units": 1, "scrap_units": 1}, None),  # together not the units of the print
        ({"good_units": 0, "scrap_units": 0}, None),
        ({"good_units": 10**30}, None),
        ([], None),
        ([1], None),
        ("1", None),
        (None, "null"),
        (None, "not json"),
        (None, ""),
        (None, '{"good_units": NaN}'),
    ],
)
def test_api_invalid_body(ready_client, body, raw):  # Review Focus 3
    a, i = finished(ready_client, units=1)
    response = api_post(ready_client, i, body, raw=raw)
    assert response.status_code == 422
    payload = response.json()
    assert set(payload) == {"error", "message"}
    assert payload["error"] in {
        "form.required", "form.invalid_number", "inspections.invalid_body", "inspections.counts_mismatch"
    }
    assert payload["message"]
    assert count(ready_client, Article, a, "stock") == 0
    assert count(ready_client, Article, a, "provisional") == 1
    assert open_count(ready_client) == 1
    assert journal_kinds(ready_client).count("inspection_good") == 0


def test_api_invalid_body_wrong_content_type(ready_client):
    a, i = finished(ready_client)
    response = api_post(ready_client, i, raw="good_units=1", content_type="application/x-www-form-urlencoded")
    assert response.status_code == 422
    assert open_count(ready_client) == 1


def test_api_unknown(ready_client):
    response = api_post(ready_client, 4242, {"good_units": 1})
    assert response.status_code == 404
    assert response.json()["error"] == "inspections.not_found"


def test_api_requires_csrf_header(ready_client):
    a, i = finished(ready_client)
    csrf_of(ready_client, PAGE)  # a session with a token exists
    assert api_post(ready_client, i, {"good_units": 1}, token=False).status_code == 403
    forged = ready_client.post(
        f"{LIST_URL}/{i}", content=json.dumps({"good_units": 1}),
        headers={"Content-Type": "application/json", "X-CSRF-Token": "forged"},
    )
    assert forged.status_code == 403
    assert open_count(ready_client) == 1
    assert count(ready_client, Article, a, "stock") == 0


def test_api_requires_login(ready_client):
    a, i = finished(ready_client)
    token = csrf_of(ready_client, PAGE)
    ready_client.cookies.clear()
    listing = ready_client.get(LIST_URL, follow_redirects=False)
    assert listing.status_code == 303 and listing.headers["location"].startswith("/login")
    assert "application/json" not in listing.headers.get("content-type", "")
    post = ready_client.post(
        f"{LIST_URL}/{i}", content=json.dumps({"good_units": 1}),
        headers={"Content-Type": "application/json", "X-CSRF-Token": token},
    )
    assert post.status_code == 403
    assert "application/json" not in post.headers.get("content-type", "")
    assert open_count(ready_client) == 1
    assert count(ready_client, Article, a, "stock") == 0


def test_browser_and_api_share_domain(ready_client):
    _, i1 = finished(ready_client, "Template A")
    _, i2 = finished(ready_client, "Template B")
    assert api_post(ready_client, i1, {"good_units": 1}).status_code == 200
    response, _ = confirm(ready_client, i2, scrap="0")
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        rows = s.scalars(select(JournalEntry).where(JournalEntry.kind == "inspection_good")).all()
        assert sorted(r.details["inspection_id"] for r in rows) == sorted([i1, i2])
        assert all(r.actor == "user" for r in rows)
        assert all(s.get(Inspection, i).resolved_at is not None for i in (i1, i2))


def test_api_form_body_with_token_field_is_refused_not_a_server_error(ready_client):
    a, i = finished(ready_client)
    response = ready_client.post(f"{LIST_URL}/{i}", data={"csrf_token": csrf_of(ready_client, PAGE), "good_units": "1"})
    assert response.status_code == 422
    assert response.json()["error"] == "inspections.invalid_body"
    assert open_count(ready_client) == 1


# ---- scrap_units: the input of the list (p88; Spec 8.2) -----------------------------------------


def test_api_resolve_by_scrap_units(ready_client):
    with sm_of(ready_client).begin() as s:
        other = make_article(s, name="Template Z").id
        make_entry(s, other, 1)
    a, i = finished(ready_client, units=3)
    response = api_post(ready_client, i, {"scrap_units": 2})
    assert response.status_code == 200
    body = response.json()
    assert (body["good"], body["scrap"]) == (1, 2)
    assert isinstance(body["replacement_entry_id"], int)
    assert count(ready_client, Article, a, "stock") == 1


def test_api_scrap_units_zero_is_all_good(ready_client):
    a, i = finished(ready_client, units=2)
    body = api_post(ready_client, i, {"scrap_units": 0}).json()
    assert (body["good"], body["scrap"], body["replacement_entry_id"]) == (2, 0, None)
    assert count(ready_client, Article, a, "stock") == 2


def test_api_both_counts_are_taken_when_they_add_up_to_the_units(ready_client):
    a, i = finished(ready_client, units=3)
    body = api_post(ready_client, i, {"good_units": 2, "scrap_units": 1}).json()
    assert (body["good"], body["scrap"]) == (2, 1)


def test_api_both_counts_that_do_not_add_up_are_refused(ready_client):
    a, i = finished(ready_client, units=3)
    response = api_post(ready_client, i, {"good_units": 1, "scrap_units": 1})
    assert response.status_code == 422
    assert response.json()["error"] == "inspections.counts_mismatch"
    assert open_count(ready_client) == 1


def test_api_list_of_one_article(ready_client):
    a, ia = finished(ready_client, "Template A")
    b, ib = finished(ready_client, "Template B")
    rows = ready_client.get(LIST_URL, params={"article": a}).json()["inspections"]
    assert [r["id"] for r in rows] == [ia]
    assert len(ready_client.get(LIST_URL).json()["inspections"]) == 2
    assert ready_client.get(LIST_URL, params={"article": "abc"}).status_code == 422


def test_api_resolved_print_answers_409_before_a_count_mismatch(ready_client):
    a, i = finished(ready_client, units=3)
    assert api_post(ready_client, i, {"scrap_units": 0}).status_code == 200
    response = api_post(ready_client, i, {"good_units": 1, "scrap_units": 1})
    assert response.status_code == 409
    assert response.json()["error"] == "inspections.already_resolved"


def test_api_list_with_an_empty_article_is_the_whole_list(ready_client):
    finished(ready_client, "Template A")
    finished(ready_client, "Template B")
    assert len(ready_client.get(LIST_URL, params={"article": ""}).json()["inspections"]) == 2
