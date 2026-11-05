import re
from datetime import date

import pytest

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, orders
from reprintbuddy.domain.catalog import PositionInput, SetInput
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.models import Article, ArticlePart, Part, ProductSet
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def act(client, path, action, page=None, **extra):
    """POST /<path>/<action> with a CSRF token and the one-time token of the form on `page`."""
    page = page or path
    data = {
        "csrf_token": csrf_of(client, page),
        "form_token": form_token_of(client, page, f"{path}/{action}"),
        **extra,
    }
    return client.post(f"{path}/{action}", data=data)


def make(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, name=fields.pop("name", "Template A"), **fields).id


def archived_at(client, model, ident):
    with sm_of(client).begin() as s:
        return s.get(model, ident).archived_at


def test_archive_article_button(bb_client):
    a = make(bb_client)
    assert f'action="/articles/{a}/archive"' in bb_client.get(f"/articles/{a}").text
    response = act(bb_client, f"/articles/{a}", "archive")
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{a}"
    page = bb_client.get(f"/articles/{a}").text  # the first page after the POST shows the message
    assert en("articles.flash_archived", name="Template A") in page
    assert en("articles.empty") in bb_client.get("/articles").text  # no longer in the active list
    archived = bb_client.get("/articles?view=archived").text
    assert "Template A" in archived
    page = bb_client.get(f"/articles/{a}").text
    assert en("catalog.archived_badge") in page and f'action="/articles/{a}/restore"' in page


def test_active_list_hides_archived(bb_client):
    a = make(bb_client, name="Template A")
    make(bb_client, name="Template B")
    act(bb_client, f"/articles/{a}", "archive")
    active = bb_client.get("/articles").text
    assert "Template B" in active and "Template A<" not in active
    assert 'href="/articles?view=archived"' in active


def test_archive_article_blocked_message(bb_client):
    a = make(bb_client, stock=2)
    with sm_of(bb_client).begin() as s:
        orders.create_order(s, reference="Order 1001", order_date=date(2026, 1, 15), items=[ArticleItem(a, 1)], actor="user")
    response = act(bb_client, f"/articles/{a}", "archive")
    assert response.status_code == 200 and en("articles.archive_open_orders") in response.text
    assert archived_at(bb_client, Article, a) is None


def test_restore_article(bb_client):
    a = make(bb_client, stock=4)
    act(bb_client, f"/articles/{a}", "archive")
    response = act(bb_client, f"/articles/{a}", "restore")
    assert response.status_code == 303
    assert "Template A" in bb_client.get("/articles").text
    with sm_of(bb_client).begin() as s:
        assert s.get(Article, a).stock == 4 and s.get(Article, a).archived_at is None


def test_restore_conflict_offers_other_file(bb_client):
    a = make(bb_client, name="Template A", library_file_id=1, plate_id=1)
    act(bb_client, f"/articles/{a}", "archive")
    make(bb_client, name="Template A2", library_file_id=1, plate_id=1)
    response = act(bb_client, f"/articles/{a}", "restore")
    assert response.status_code == 200
    assert en("articles.restore_plate_taken", name="Template A2") in response.text
    assert f'href="/articles/{a}/relink"' in response.text and en("articles.choose_other_file") in response.text
    # other file: browse, choose Template B, plate 2
    browse = bb_client.get(f"/articles/{a}/relink?folder=1")
    assert browse.status_code == 200 and f"/articles/{a}/relink/file/2" in browse.text
    assert bb_client.get(f"/articles/{a}/relink").status_code == 200
    plates = bb_client.get(f"/articles/{a}/relink/file/2")
    assert plates.status_code == 200 and f'action="/articles/{a}/relink/file/2/plate/2"' in plates.text
    path = f"/articles/{a}/relink/file/2/plate/2"
    data = {"csrf_token": csrf_of(bb_client, f"/articles/{a}/relink/file/2"),
            "form_token": form_token_of(bb_client, f"/articles/{a}/relink/file/2", path)}
    done = bb_client.post(path, data=data)
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{a}"
    with sm_of(bb_client).begin() as s:
        art = s.get(Article, a)
        assert art.archived_at is None and art.plate_id == 2 and art.library_file_id == 2
        assert art.file_name == "Template B.gcode.3mf"


def test_relink_plate_taken_and_missing_file(bb_client, fake_bambuddy):
    a = make(bb_client, name="Template A", library_file_id=1, plate_id=1)
    act(bb_client, f"/articles/{a}", "archive")
    make(bb_client, name="Template B plate 2", library_file_id=2, plate_id=2)
    page = f"/articles/{a}/relink/file/2"
    path = f"{page}/plate/2"
    data = {"csrf_token": csrf_of(bb_client, page), "form_token": form_token_of(bb_client, page, f"{page}/plate/1")}
    taken = bb_client.post(path, data=data)
    assert taken.status_code == 200 and en("articles.plate_taken", name="Template B plate 2") in taken.text
    fake_bambuddy.remove_file(2)
    gone = bb_client.post(f"{page}/plate/1", data=data)
    assert gone.status_code == 200 and en("articles.file_missing") in gone.text
    with sm_of(bb_client).begin() as s:
        assert s.get(Article, a).archived_at is not None


def test_relink_only_for_archived_articles(bb_client):
    a = make(bb_client)
    assert bb_client.get(f"/articles/{a}/relink").status_code == 404
    assert bb_client.get("/articles/999/relink").status_code == 404


def test_archive_part_in_use_message(bb_client):
    with sm_of(bb_client).begin() as s:
        art = make_article(s, name="Template A")
        p = make_part(s, name="Insert M4").id
        s.add(ArticlePart(article_id=art.id, part_id=p, qty_per_unit=2))
    response = act(bb_client, f"/parts/{p}", "archive", page=f"/parts/{p}/edit")
    assert response.status_code == 200 and en("parts.archive_in_use", article="Template A") in response.text
    assert archived_at(bb_client, Part, p) is None


def test_archive_restore_part(bb_client):
    p = None
    with sm_of(bb_client).begin() as s:
        p = make_part(s, name="Insert M4", stock=7).id
    response = act(bb_client, f"/parts/{p}", "archive", page=f"/parts/{p}/edit")
    assert response.status_code == 303
    assert en("parts.empty") in bb_client.get("/parts").text
    assert "Insert M4" in bb_client.get("/parts?view=archived").text
    page = bb_client.get(f"/parts/{p}/edit").text
    assert en("catalog.archived_badge") in page
    assert act(bb_client, f"/parts/{p}", "restore", page=f"/parts/{p}/edit").status_code == 303
    assert "Insert M4" in bb_client.get("/parts").text
    with sm_of(bb_client).begin() as s:
        assert s.get(Part, p).stock == 7 and s.get(Part, p).archived_at is None


def test_archive_restore_set(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A").id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])])).id
    assert act(bb_client, f"/sets/{set_id}", "archive", page=f"/sets/{set_id}/edit").status_code == 303
    assert "Starter Set" in bb_client.get("/sets?view=archived").text
    assert en("sets.empty") in bb_client.get("/sets").text
    assert act(bb_client, f"/sets/{set_id}", "restore", page=f"/sets/{set_id}/edit").status_code == 303
    assert "Starter Set" in bb_client.get("/sets").text
    assert archived_at(bb_client, ProductSet, set_id) is None


def test_double_archive_click_is_one_action(bb_client):
    a = make(bb_client)
    page = f"/articles/{a}"
    data = {"csrf_token": csrf_of(bb_client, page), "form_token": form_token_of(bb_client, page, f"{page}/archive")}
    assert bb_client.post(f"{page}/archive", data=data).status_code == 303
    second = bb_client.post(f"{page}/archive", data=data)
    assert second.status_code == 303
    assert en("form.duplicate") in bb_client.get(page).text


def test_unknown_ids_are_404(bb_client):
    for kind in ("articles", "parts", "sets"):
        for action in ("archive", "restore"):
            data = {"csrf_token": csrf_of(bb_client, "/articles"), "form_token": "x"}
            assert bb_client.post(f"/{kind}/999/{action}", data=data).status_code == 404, (kind, action)


def test_archived_absent_from_selections(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1, stock=3).id
        b = make_article(s, name="Template B", library_file_id=2, plate_id=1, stock=3).id
        insert = make_part(s, name="Insert M4").id
        hex_key = make_part(s, name="Hex key 2").id
        starter = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])])).id
        other = catalog.create_set(s, SetInput("Other Set", None, [PositionInput(1, [a])])).id
        catalog.archive_set(s, starter)
        catalog.archive_article(s, b)
        catalog.archive_part(s, hex_key)
    form = bb_client.get("/articles/new/file/1/plate/1").text
    assert f'name="part_{insert}"' in form and f'name="part_{hex_key}"' not in form
    new_set = bb_client.get("/sets/new").text
    assert f'<option value="{a}"' in new_set and f'<option value="{b}"' not in new_set
    new_order = bb_client.get("/orders/new").text
    assert f'value="article:{a}"' in new_order and f'value="article:{b}"' not in new_order
    assert f'value="set:{other}"' in new_order and f'value="set:{starter}"' not in new_order
    bookings = bb_client.get("/bookings").text
    assert f'<option value="{a}"' in bookings and f'<option value="{b}"' not in bookings
    assert f'<option value="{insert}"' in bookings and f'<option value="{hex_key}"' not in bookings


def test_set_with_archived_article_is_not_orderable_in_form(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])])).id
        catalog.archive_article(s, a)
    assert f'value="set:{set_id}"' not in bb_client.get("/orders/new").text


def test_choice_form_offers_only_active_options(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
        b = make_article(s, name="Template B", library_file_id=2, plate_id=1).id
        c = make_article(s, name="Template C", library_file_id=3, plate_id=1).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a, b, c])])).id
        catalog.archive_article(s, c)
    data = {
        "csrf_token": csrf_of(bb_client, "/orders/new"),
        "form_token": form_token_of(bb_client, "/orders/new", "/orders/new"),
        "reference": "Order 1001", "order_date": "2026-01-15", "note": "", "rows": "5",
        "item_0": f"set:{set_id}", "qty_0": "1",
    }
    response = bb_client.post("/orders/new", data=data)
    assert response.status_code == 200
    choice = re.search(r'<select name="choice_0_\d+">(.*?)</select>', response.text, re.S).group(1)
    assert f'value="{a}"' in choice and f'value="{b}"' in choice and f'value="{c}"' not in choice


def test_archived_booking_post_rejected(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A", stock=5).id
        p = make_part(s, name="Insert M4", stock=5).id
        catalog.archive_article(s, a)
        catalog.archive_part(s, p)
    token = form_token_of(bb_client, "/bookings", "/bookings/sale")
    csrf = csrf_of(bb_client, "/bookings")
    for action, fields in (
        ("sale", {"article_id": a, "qty": "1"}),
        ("stock-in", {"article_id": a, "qty": "1"}),
        ("inventory", {"article_id": a, "field": "stock", "counted": "1"}),
    ):
        response = bb_client.post(f"/bookings/{action}", data={"csrf_token": csrf, "form_token": token, **fields})
        assert response.status_code == 200 and en("articles.not_found") in response.text, action
    for action, fields in (("part-receipt", {"part_id": p, "qty": "1"}), ("part-inventory", {"part_id": p, "counted": "1"})):
        response = bb_client.post(f"/bookings/{action}", data={"csrf_token": csrf, "form_token": token, **fields})
        assert response.status_code == 200 and en("parts.not_found") in response.text, action
    with sm_of(bb_client).begin() as s:
        assert s.get(Article, a).stock == 5 and s.get(Part, p).stock == 5


def test_archive_requires_csrf(bb_client):
    a = make(bb_client)
    for kind in ("articles", "parts", "sets"):
        for action in ("archive", "restore"):
            assert bb_client.post(f"/{kind}/{a}/{action}", data={"form_token": "x"}).status_code == 403
    assert bb_client.post(f"/articles/{a}/relink/file/1/plate/1", data={"form_token": "x"}).status_code == 403


def test_archive_german(bb_client):
    a = make(bb_client)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = bb_client.get(f"/articles/{a}").text
    assert "Archivieren" in page
    assert "Archiviert" in bb_client.get("/articles").text
    act(bb_client, f"/articles/{a}", "archive")
    assert "Wiederherstellen" in bb_client.get(f"/articles/{a}").text


def test_archived_article_edit_is_refused(bb_client):
    a = make(bb_client)
    act(bb_client, f"/articles/{a}", "archive")
    page = bb_client.get(f"/articles/{a}/edit")
    assert page.status_code == 200 and en("articles.edit_archived") in page.text
    assert 'name="units_per_print"' not in page.text
    assert f'href="/articles/{a}/edit"' not in page.text
    data = {"csrf_token": csrf_of(bb_client, f"/articles/{a}"), "name": "Renamed", "sku": "", "units_per_print": "1",
            "min_stock": "0", "buffer": "0", "target": "model:A1"}
    response = bb_client.post(f"/articles/{a}/edit", data=data)
    assert response.status_code == 200 and en("articles.edit_archived") in response.text
    with sm_of(bb_client).begin() as s:
        assert s.get(Article, a).name == "Template A"
    act(bb_client, f"/articles/{a}", "restore")
    assert 'name="units_per_print"' in bb_client.get(f"/articles/{a}/edit").text
