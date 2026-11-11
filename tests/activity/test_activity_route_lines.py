"""A line for every route that writes one (Spec 10.7): each test posts through the test client as the page's
form does (CSRF token, one-time token where the form has one, the field names of the form) and then
looks for the line in the log. `test_activity_routes.py` lists the routes and the tests that show them.

Routes that are shown elsewhere in this folder (sign-in, backup, restore, update, notify, labels, upload)
are named there.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_filament_settings, make_part, make_set, make_sort
from reprintbuddy.models import ActivityEntry, Article, ArticleFilament, ArticlePart, FilamentSort, ReorderMark, utcnow


def lines_of(client, *kinds: str) -> list[ActivityEntry]:
    """The lines written so far (the ones of `activity.record` need the drain first)."""
    client.app.state.activity.drain()
    with sm_of(client)() as s:
        rows = list(s.scalars(select(ActivityEntry).order_by(ActivityEntry.id)))
    return [row for row in rows if not kinds or row.kind in kinds]


def the_line(client, kind: str) -> ActivityEntry:
    found = lines_of(client, kind)
    assert len(found) == 1, [(row.kind, row.details) for row in lines_of(client)]
    assert found[0].actor == "user"
    return found[0]


def new_settings_line(client, before: int) -> ActivityEntry:
    """The one `settings_changed` line written since `before` lines (the fixtures wrote some)."""
    found = lines_of(client, "settings_changed")
    assert len(found) == before + 1, [row.details for row in found]
    return found[-1]


def setting_names(line: ActivityEntry) -> dict[str, dict]:
    assert line.actor == "user"
    return {item["setting"]: item for item in line.details["settings"]}


def article_of(client, **fields) -> int:
    with sm_of(client).begin() as s:
        return make_article(s, name=fields.pop("name", "Bracket"), **fields).id


def part_of(client, **fields) -> int:
    with sm_of(client).begin() as s:
        return make_part(s, name=fields.pop("name", "Insert"), **fields).id


def form_post(client, action, page=None, **data):
    """POST `action` as the form of `page` would: both tokens, then the fields."""
    page = page or action
    return client.post(
        action,
        data={"csrf_token": csrf_of(client, page), "form_token": form_token_of(client, page, action), **data},
    )


# ---- articles --------------------------------------------------------------------------------------


def test_a_new_article_is_created_by_the_user(bb_client):
    path = "/articles/new/file/1/plate/1"
    response = form_post(
        bb_client, path, name="Bracket", sku="", units_per_print="1", min_stock="0", buffer="0", target="model:A1"
    )
    assert response.status_code == 303
    with sm_of(bb_client)() as s:
        created = s.scalars(select(Article)).one()
    assert the_line(bb_client, "article_created").details == {"id": created.id, "name": "Bracket"}


def test_an_article_archived_and_restored_by_the_user(bb_client):
    a = article_of(bb_client)
    page = f"/articles/{a}"
    assert form_post(bb_client, f"{page}/archive", page).status_code == 303
    assert the_line(bb_client, "article_archived").details == {"id": a, "name": "Bracket"}
    assert form_post(bb_client, f"{page}/restore", page).status_code == 303
    assert the_line(bb_client, "article_restored").details == {"id": a, "name": "Bracket"}


def test_the_automation_of_an_article_switched_in_the_list(bb_client):
    a = article_of(bb_client, target_model="A1")
    response = form_post(bb_client, f"/articles/{a}/auto", "/articles", enabled="1", next="/articles")
    assert response.status_code == 303
    assert the_line(bb_client, "article_auto").details == {"id": a, "name": "Bracket", "on": True}


def test_an_article_changed_on_its_edit_page(ready_client):
    a = article_of(ready_client, target_model="A1")
    response = ready_client.post(
        f"/articles/{a}/edit",
        data={
            "csrf_token": csrf_of(ready_client, f"/articles/{a}/edit"),
            "name": "Bracket 2", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
            "target": "model:A1",
        },
    )
    assert response.status_code == 303
    assert the_line(ready_client, "article_changed").details == {"id": a, "name": "Bracket 2", "fields": ["name"]}


def test_the_filaments_of_an_article_saved_on_its_filament_page(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", brand="Brand A", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Bracket").id
        s.add(ArticleFilament(article_id=a, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
    page = f"/articles/{a}/filaments"
    bb_client.get(page)  # shows the page once: this enters the spool sorts
    with sm_of(bb_client)() as s:
        sort = s.scalars(select(FilamentSort).where(FilamentSort.brand == "Brand A")).one().id
    response = bb_client.post(page, data={"csrf_token": csrf_of(bb_client, page), "slot": "1", "tick_1": str(sort)})
    assert response.status_code == 303
    assert the_line(bb_client, "article_changed").details == {"id": a, "name": "Bracket", "fields": ["filaments"]}


def test_the_target_quantity_saved_in_the_list(bb_client):
    a = article_of(bb_client, min_stock=2, buffer=1)
    response = form_post(bb_client, f"/articles/{a}/target", "/articles", target="6", next="/articles")
    assert response.status_code == 303
    assert the_line(bb_client, "article_target").details == {"id": a, "name": "Bracket"}


def test_an_archived_article_restored_with_another_plate(bb_client):
    a = article_of(bb_client, library_file_id=1, plate_id=1, archived_at=utcnow())
    page = f"/articles/{a}/relink/file/2"
    response = form_post(bb_client, f"{page}/plate/1", page)
    assert response.status_code == 303
    assert the_line(bb_client, "article_restored").details == {"id": a, "name": "Bracket"}


# ---- spool sorts, parts, sets ----------------------------------------------------------------------


def test_the_order_settings_of_a_spool_sort(ready_client):
    with sm_of(ready_client).begin() as s:
        sort = make_sort(s)
        sort_id, label = sort.id, sort.label
    response = ready_client.post(
        f"/filament/{sort_id}",
        data={
            "csrf_token": csrf_of(ready_client, f"/filament/{sort_id}"),
            "lead_time_days": "5", "safety_value": "300", "safety_unit": "grams", "pack_size": "1000",
            "min_order_packs": "1", "supplier": "Example Supplier", "order_url": "https://shop.example/pla-black",
        },
    )
    assert response.status_code == 303
    assert the_line(ready_client, "filament_settings_changed").details == {"id": sort_id, "name": label}


PART_FIELDS = {
    "name": "Insert", "unit": "pcs", "min_stock": "0", "pack_size": "50", "min_order_packs": "1",
    "lead_time_days": "5", "safety_value": "10", "safety_unit": "qty", "supplier": "Example Supplier",
    "order_url": "https://shop.example/insert",
}


def test_a_part_created_changed_archived_and_restored(ready_client):
    response = form_post(ready_client, "/parts/new", initial_stock="50", **PART_FIELDS)
    assert response.status_code == 303
    with sm_of(ready_client)() as s:
        from reprintbuddy.models import Part

        p = s.scalars(select(Part)).one().id
    assert the_line(ready_client, "part_created").details == {"id": p, "name": "Insert"}

    edit = f"/parts/{p}/edit"
    response = ready_client.post(edit, data={"csrf_token": csrf_of(ready_client, edit), **PART_FIELDS, "name": "Insert 2"})
    assert response.status_code == 303
    assert the_line(ready_client, "part_changed").details == {"id": p, "name": "Insert 2", "fields": ["name"]}

    assert form_post(ready_client, f"/parts/{p}/archive", edit).status_code == 303
    assert the_line(ready_client, "part_archived").details == {"id": p, "name": "Insert 2"}
    assert form_post(ready_client, f"/parts/{p}/restore", edit).status_code == 303
    assert the_line(ready_client, "part_restored").details == {"id": p, "name": "Insert 2"}


def test_a_set_created_changed_archived_and_restored(ready_client):
    a = article_of(ready_client, name="Bracket A", stock=3)
    b = article_of(ready_client, name="Bracket B", stock=3)
    response = form_post(
        ready_client, "/sets/new", name="Kit", sku="K-1", base_rows="3",
        base_0_qty="1", base_0_article=str(a), base_1_qty="2", base_1_article=str(b),
    )
    assert response.status_code == 303
    with sm_of(ready_client)() as s:
        from reprintbuddy.models import ProductSet

        sid = s.scalars(select(ProductSet)).one().id
    assert the_line(ready_client, "set_created").details == {"id": sid, "name": "Kit"}

    edit = f"/sets/{sid}/edit"
    response = ready_client.post(
        edit,
        data={"csrf_token": csrf_of(ready_client, edit), "name": "Kit 2", "sku": "K-1", "base_rows": "3",
              "base_0_qty": "1", "base_0_article": str(a), "base_1_qty": "2", "base_1_article": str(b)},
    )
    assert response.status_code == 303
    assert the_line(ready_client, "set_changed").details == {"id": sid, "name": "Kit 2", "fields": ["name"]}

    assert form_post(ready_client, f"/sets/{sid}/archive", edit).status_code == 303
    assert the_line(ready_client, "set_archived").details == {"id": sid, "name": "Kit 2"}
    assert form_post(ready_client, f"/sets/{sid}/restore", edit).status_code == 303
    assert the_line(ready_client, "set_restored").details == {"id": sid, "name": "Kit 2"}


# ---- reorder ---------------------------------------------------------------------------------------


def test_a_reorder_marked_and_received_by_hand(bb_client):
    with sm_of(bb_client).begin() as s:
        part = make_part(
            s, name="Insert", stock=10, pack_size=50, min_order_packs=1, lead_time_days=0, safety_value=20,
            safety_unit="qty", supplier="Example Supplier", order_url="https://shop.example/insert",
        )
        art = make_article(s, name="Bracket", needs_assembly=True, stock=0, min_stock=3, buffer=0)
        s.add(ArticlePart(article_id=art.id, part_id=part.id, qty_per_unit=2))
        part_id = part.id
    response = form_post(bb_client, "/reorder/mark", "/reorder", item_type="part", item_id=str(part_id), packs="1")
    assert response.status_code == 303
    ref = {"type": "part", "id": part_id, "name": "Insert"}
    assert the_line(bb_client, "reorder_ordered").details == ref

    with sm_of(bb_client)() as s:
        mark_id = s.scalars(select(ReorderMark)).one().id
    assert form_post(bb_client, f"/reorder/marks/{mark_id}/received", "/reorder").status_code == 303
    assert the_line(bb_client, "reorder_received").details == ref


def test_a_reorder_taken_back_after_the_question(bb_client):
    """Spec 9.4, 10.7 (p280): the question writes nothing; the confirmation writes "taken back"."""
    with sm_of(bb_client).begin() as s:
        part = make_part(
            s, name="Insert", stock=10, pack_size=50, min_order_packs=1, lead_time_days=0, safety_value=20,
            safety_unit="qty", supplier="Example Supplier", order_url="https://shop.example/insert",
        )
        art = make_article(s, name="Bracket", needs_assembly=True, stock=0, min_stock=3, buffer=0)
        s.add(ArticlePart(article_id=art.id, part_id=part.id, qty_per_unit=2))
        part_id = part.id
    assert form_post(bb_client, "/reorder/mark", "/reorder", item_type="part", item_id=str(part_id), packs="1").status_code == 303
    with sm_of(bb_client)() as s:
        mark_id = s.scalars(select(ReorderMark)).one().id
    action = f"/reorder/marks/{mark_id}/withdraw"
    token = form_token_of(bb_client, "/reorder", action)
    asked = bb_client.post(action, data={"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token})
    assert asked.status_code == 200 and lines_of(bb_client, "reorder_withdrawn") == []
    done = bb_client.post(action, data={"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "confirm": "yes"})
    assert done.status_code == 303
    assert the_line(bb_client, "reorder_withdrawn").details == {
        "type": "part", "id": part_id, "name": "Insert", "packs": 1, "size": 50, "unit": "pcs",
        "supplier": "Example Supplier", "supplier_position": 0, "arrived": 0,
    }
    assert lines_of(bb_client, "reorder_received") == []


# ---- settings --------------------------------------------------------------------------------------


def test_the_automation_settings_saved_on_the_page(ready_client):
    before = len(lines_of(ready_client, "settings_changed"))
    response = ready_client.post(
        "/settings/automation",
        data={"csrf_token": csrf_of(ready_client, "/settings"), "automation": "on", "queue_cap": "7", "poll_minutes": "15"},
    )
    assert response.status_code == 303
    names = setting_names(new_settings_line(ready_client, before))
    assert names["automation"] == {"setting": "automation", "old": False, "new": True}
    assert names["queue_cap"] == {"setting": "queue_cap", "old": 10, "new": 7}
    assert "poll_minutes" not in names  # unchanged


def test_the_main_switch_of_the_automation_on_the_overview(ready_client):
    before = len(lines_of(ready_client, "settings_changed"))
    action = "/settings/automation/main"
    response = ready_client.post(
        action,
        data={"csrf_token": csrf_of(ready_client, "/"), "form_token": form_token_of(ready_client, "/", action),
              "enabled": "1", "next": "/"},
    )
    assert response.status_code == 303
    names = setting_names(new_settings_line(ready_client, before))
    assert names == {"automation": {"setting": "automation", "old": False, "new": True}}


def test_the_address_in_the_qr_codes_saved_on_the_page(ready_client):
    before = len(lines_of(ready_client, "settings_changed"))
    response = ready_client.post(
        "/settings/qr", data={"csrf_token": csrf_of(ready_client, "/settings"), "qr_base_url": "http://reprint.test:8080"}
    )
    assert response.status_code == 303
    item = setting_names(new_settings_line(ready_client, before))["qr_base_url"]
    assert item["new"] == "http://reprint.test:8080"


def test_the_label_settings_saved_on_the_page(ready_client):
    before = len(lines_of(ready_client, "settings_changed"))
    response = ready_client.post(
        "/settings/labels",
        data={"csrf_token": csrf_of(ready_client, "/settings"), "label_printer_host": "198.51.100.20",
              "label_roll": "DK-11209", "label_field": ["name", "sku"]},
    )
    assert response.status_code == 303
    names = setting_names(new_settings_line(ready_client, before))
    assert names["label_printer_host"]["new"] == "198.51.100.20"
    assert names["label_fields"] == {"setting": "label_fields", "change": "changed"}


def test_the_automatic_check_for_versions_switched_on_the_page(ops_env):
    """The switch sits on the update card of a git installation and uses up a one-time token (p99)."""
    from reprintbuddy.ops.runner import CommandResult

    (ops_env.install_dir / ".git").mkdir(parents=True, exist_ok=True)
    d = str(ops_env.install_dir)
    ops_env.runner.on(("git", "-C", d, "describe"), CommandResult(0, "v0.1.0\n"))
    ops_env.runner.on(("git", "-C", d, "ls-remote"), CommandResult(0, "abc\trefs/tags/v0.1.0\n"))
    client = ops_env.client
    page = "/settings?tab=maintenance"
    before = len(lines_of(client, "settings_changed"))
    response = client.post(
        "/settings/update/auto",
        data={"csrf_token": csrf_of(client, page), "form_token": form_token_of(client, page, "/settings/update/auto"),
              "enabled": "0"},
    )
    assert response.status_code == 303
    item = setting_names(new_settings_line(client, before))["update_check"]
    assert item["new"] is False


def test_the_bambuddy_connection_of_the_setup(admin_client, respx_mock):
    from conftest import mock_auth_me

    mock_auth_me(respx_mock)
    before = len(lines_of(admin_client, "settings_changed"))
    response = admin_client.post(
        "/setup/bambuddy/connect",
        data={
            "csrf_token": csrf_of(admin_client, "/setup/bambuddy/connect"),
            "bambuddy_address": "http://bambuddy.test:8000",
            "bambuddy_token": "bb_SETUPKEY_123456",
        },
    )
    assert response.status_code == 303  # on to the page with the result (p179, p192)
    line = new_settings_line(admin_client, before)
    names = setting_names(line)
    assert names["bambuddy_url"]["new"] == "http://bambuddy.test:8000"
    assert "bambuddy_key" in names
    assert "SETUPKEY" not in repr(line.details)
