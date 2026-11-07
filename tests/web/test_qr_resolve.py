"""The fixed address of an article, `/a/<id>`, that the QR code carries (Spec 8.4).

It sends to "To check" for the article when prints wait for inspection, else to "Assembly" for the
article when units wait for assembly, else to the article's own page.
"""

from datetime import UTC, datetime

from conftest import ADMIN, PASSWORD, csrf_of, sm_of
from factories import make_article, make_dispatch
from sqlalchemy import select

from reprintbuddy.domain import tracking
from reprintbuddy.models import Article, Inspection


def article(client, name="Template A", *, awaiting=0, archived=False, inspections=0):
    with sm_of(client).begin() as s:
        a = make_article(s, name=name, awaiting_assembly=awaiting)
        for _ in range(inspections):
            dispatch = make_dispatch(s, a.id, status="queued", units=1)
            assert tracking.apply_outcome(s, dispatch.id, "completed", "completed")
        if archived:
            a.archived_at = datetime.now(UTC)
        return a.id


def open_inspections(client, article_id):
    with sm_of(client).begin() as s:
        return len(s.scalars(select(Inspection).where(Inspection.article_id == article_id)).all())


def go(client, article_id):
    response = client.get(f"/a/{article_id}")
    assert response.status_code == 303, response.status_code
    assert response.headers["cache-control"] == "no-store"
    return response.headers["location"]


def test_prints_to_check_open_the_inspection_list_of_the_article(ready_client):
    a = article(ready_client, inspections=2)
    assert open_inspections(ready_client, a) == 2
    assert go(ready_client, a) == f"/inspection?article={a}"


def test_units_for_assembly_open_the_assembly_list_of_the_article(ready_client):
    a = article(ready_client, awaiting=3)
    assert go(ready_client, a) == f"/assembly?article={a}"


def test_inspection_goes_before_assembly(ready_client):
    a = article(ready_client, awaiting=3, inspections=1)
    assert go(ready_client, a) == f"/inspection?article={a}"


def test_nothing_waiting_opens_the_article_page(ready_client):
    a = article(ready_client)
    assert go(ready_client, a) == f"/articles/{a}"


def test_only_this_article_counts(ready_client):
    other = article(ready_client, "Template B", inspections=1, awaiting=2)
    a = article(ready_client)
    assert go(ready_client, a) == f"/articles/{a}"
    assert go(ready_client, other) == f"/inspection?article={other}"


def test_archived_article_with_a_print_to_check_still_opens_the_list(ready_client):
    a = article(ready_client, inspections=1, archived=True)
    assert go(ready_client, a) == f"/inspection?article={a}"


def test_archived_article_with_units_for_assembly_opens_its_page(ready_client):
    # the assembly list shows no archived article: the link must not lead to an empty list
    a = article(ready_client, awaiting=2, archived=True)
    assert go(ready_client, a) == f"/articles/{a}"


def test_the_target_follows_the_state_at_every_call(ready_client):
    a = article(ready_client, awaiting=1)
    assert go(ready_client, a) == f"/assembly?article={a}"
    with sm_of(ready_client).begin() as s:
        s.get(Article, a).awaiting_assembly = 0
    assert go(ready_client, a) == f"/articles/{a}"


def test_unknown_article_is_not_found(ready_client):
    assert ready_client.get("/a/999").status_code == 404
    assert ready_client.get("/a/" + "9" * 19).status_code == 404  # longer than any id
    assert ready_client.get("/a/abc").status_code == 404
    assert ready_client.get("/a/0").status_code == 404


def test_only_get(ready_client):
    a = article(ready_client)
    assert ready_client.post(f"/a/{a}", data={"csrf_token": csrf_of(ready_client, "/articles")}).status_code == 405


def test_signed_out_goes_to_the_login_and_back_to_the_article_address(ready_client):
    a = article(ready_client, inspections=1)
    ready_client.cookies.clear()
    response = ready_client.get(f"/a/{a}")
    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=%2Fa%2F{a}"
    token = csrf_of(ready_client, f"/login?next=%2Fa%2F{a}")
    login = ready_client.post(
        "/login", data={"csrf_token": token, "username": ADMIN, "password": PASSWORD, "next": f"/a/{a}"}
    )
    assert login.status_code == 303 and login.headers["location"] == f"/a/{a}"
    assert go(ready_client, a) == f"/inspection?article={a}"


def test_signed_out_learns_nothing_about_the_article(ready_client):
    known_id = article(ready_client)
    ready_client.cookies.clear()
    known = ready_client.get(f"/a/{known_id}")
    unknown = ready_client.get("/a/999")
    assert known.status_code == unknown.status_code == 303
    assert known.headers["location"] == f"/login?next=%2Fa%2F{known_id}"
    assert unknown.headers["location"] == "/login?next=%2Fa%2F999"
