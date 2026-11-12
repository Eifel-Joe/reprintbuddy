"""Sets as base parts in the set form, and inner sets in the set list (Spec 5.4, 10.1; 0.4.3 p423)."""

import re
from datetime import datetime

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_set
from reprintbuddy.models import ProductSet, SetPositionSet
from reprintbuddy.web.i18n import translate

ARCHIVED = datetime(2026, 11, 12, 8, 0)


def en(key, **params):
    return translate("en", key, **params)


def inner(set_id, *key):
    return ("set", set_id, tuple(key))


def kits(client):
    """Basic kit EX-GA (2 holders + 1 manual), extension (plate + screw set), complete kit (both),
    sizes EX-SZ (frame, choice small -S / large -L), other (lid + manual)."""
    with sm_of(client).begin() as s:
        a = {name: make_article(s, name=name, stock=5).id
             for name in ("Holder", "Manual", "Plate", "Screw set", "Frame", "Small", "Large", "Lid")}
        ids = {
            "basic": make_set(s, "Basic kit", [(2, a["Holder"]), (1, a["Manual"])], sku="EX-GA").id,
            "extension": make_set(s, "Extension", [(1, a["Plate"]), (1, a["Screw set"])]).id,
            "sizes": make_set(s, "Sizes", [(1, a["Frame"])], [(1, [(a["Small"], "-S"), (a["Large"], "-L")])], sku="EX-SZ").id,
            "other": make_set(s, "Other", [(1, a["Lid"]), (1, a["Manual"])]).id,
        }
        ids["complete"] = make_set(s, "Complete kit", [(1, inner(ids["basic"])), (1, inner(ids["extension"]))], sku="EX-KP").id
    return a, ids


def form(client, url="/sets/new", **rows):
    data = {
        "csrf_token": csrf_of(client, url),
        "name": "Workshop kit", "sku": "", "base_rows": "3", "c1_rows": "3", "c1_qty": "1",
        "base_0_qty": "1", "base_1_qty": "1", "base_2_qty": "1",
    }
    if url == "/sets/new":
        data["form_token"] = form_token_of(client, url, url)
    data.update(rows)
    return data


def select_html(page: str, name: str) -> str:
    return re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', page, re.S).group(1)


def option(page: str, name: str, value: str) -> str | None:
    found = re.search(rf'<option value="{value}"[^>]*>[^<]*</option>', select_html(page, name))
    return found.group(0) if found else None


def test_base_drop_down_has_groups_articles_and_sets(ready_client):
    a, ids = kits(ready_client)
    page = ready_client.get("/sets/new").text
    base = select_html(page, "base_0_article")
    assert f'<optgroup label="{en("sets.group_articles")}">' in base and f'<optgroup label="{en("sets.group_sets")}">' in base
    assert ">Basic kit · EX-GA<" in option(page, "base_0_article", f"set:{ids['basic']}")
    assert ">Extension<" in option(page, "base_0_article", f"set:{ids['extension']}")
    assert "set:" not in select_html(page, "c1_0_article") and "optgroup" not in select_html(page, "c1_0_article")


def test_set_with_choices_is_offered_once_per_variant(ready_client):
    a, ids = kits(ready_client)
    page = ready_client.get("/sets/new").text
    assert ">Sizes · EX-SZ-S (Small)<" in option(page, "base_0_article", f"set:{ids['sizes']}:{a['Small']}")
    assert ">Sizes · EX-SZ-L (Large)<" in option(page, "base_0_article", f"set:{ids['sizes']}:{a['Large']}")
    assert option(page, "base_0_article", f"set:{ids['sizes']}") is None


def test_own_set_and_sets_holding_it_are_not_offered(ready_client):
    a, ids = kits(ready_client)
    page = ready_client.get(f"/sets/{ids['basic']}/edit").text
    assert option(page, "base_0_article", f"set:{ids['basic']}") is None
    assert option(page, "base_0_article", f"set:{ids['complete']}") is None
    assert option(page, "base_2_article", f"set:{ids['extension']}") is not None


def test_archived_and_incomplete_sets_are_not_offered(ready_client):
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, ids["extension"]).archived_at = ARCHIVED
        lonely = make_set(s, "Lonely", [(1, a["Lid"])]).id
    page = ready_client.get("/sets/new").text
    assert option(page, "base_0_article", f"set:{ids['extension']}") is None
    assert option(page, "base_0_article", f"set:{lonely}") is None


def test_chosen_inner_set_hides_its_articles_and_sets_sharing_them(ready_client):  # Spec 5.4
    a, ids = kits(ready_client)
    page = ready_client.post("/sets/new", data=form(ready_client, base_0_article=f"set:{ids['basic']}", action="more_base")).text
    assert "selected" in option(page, "base_0_article", f"set:{ids['basic']}")
    for name in ("base_1_article", "c1_0_article"):
        assert re.search(rf'<option value="{a["Holder"]}" disabled hidden>', select_html(page, name)), name
        assert re.search(rf'<option value="{a["Plate"]}">', select_html(page, name)), name
    assert "disabled hidden" in option(page, "base_1_article", f"set:{ids['other']}")  # shares the manual
    assert "disabled hidden" in option(page, "base_1_article", f"set:{ids['basic']}")  # the same set
    assert "disabled hidden" in option(page, "base_1_article", f"set:{ids['complete']}")  # holds it
    assert "disabled hidden" not in option(page, "base_1_article", f"set:{ids['extension']}")


def test_contains_hint_under_a_chosen_set(ready_client):  # Spec 5.4
    a, ids = kits(ready_client)
    page = ready_client.post("/sets/new", data=form(ready_client, base_0_article=f"set:{ids['basic']}", action="more_base")).text
    hint = en("sets.contains", items="2 × Holder, 1 × Manual")
    assert f">{hint}</p>" in page
    assert f'data-contains="{hint}"' in option(page, "base_1_article", f"set:{ids['basic']}")


def test_create_set_of_two_sets_through_the_form(ready_client):  # the user's case (p423)
    a, ids = kits(ready_client)
    data = form(ready_client, base_0_article=f"set:{ids['basic']}", base_1_article=f"set:{ids['sizes']}:{a['Large']}")
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    with sm_of(ready_client).begin() as s:
        rows = s.scalars(select(SetPositionSet).order_by(SetPositionSet.position_id)).all()
        assert [(r.set_id, r.key) for r in rows][-2:] == [(ids["basic"], ()), (ids["sizes"], (a["Large"],))]


def test_doubled_article_without_script_names_it(ready_client):  # review focus 5
    a, ids = kits(ready_client)
    data = form(ready_client, base_0_article=f"set:{ids['basic']}", base_1_article=str(a["Holder"]))
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200 and en("sets.article_twice", name="Holder") in response.text
    assert 'value="Workshop kit"' in response.text


def test_same_set_twice_without_script(ready_client):
    a, ids = kits(ready_client)
    data = form(ready_client, base_0_article=f"set:{ids['sizes']}:{a['Small']}", base_1_article=f"set:{ids['sizes']}:{a['Large']}")
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200 and en("sets.set_twice", name="Sizes") in response.text


def test_edit_form_names_sets_it_is_part_of(ready_client):  # Spec 5.4: "Steckt in"
    a, ids = kits(ready_client)
    assert en("sets.part_of", names="Complete kit") in ready_client.get(f"/sets/{ids['basic']}/edit").text
    assert en("sets.part_of", names="").split(":")[0] not in ready_client.get(f"/sets/{ids['complete']}/edit").text


def test_edit_form_keeps_inner_set_and_variant_selected(ready_client):
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        bundle = make_set(s, "Bundle", [(1, inner(ids["sizes"], a["Small"])), (1, a["Plate"])]).id
    page = ready_client.get(f"/sets/{bundle}/edit").text
    assert "selected" in option(page, "base_0_article", f"set:{ids['sizes']}:{a['Small']}")
    assert f">{en('sets.contains', items='1 × Frame, 1 × Small')}</p>" in page
    page = ready_client.get(f"/sets/{ids['complete']}/edit").text
    assert "selected" in option(page, "base_0_article", f"set:{ids['basic']}")
    assert "selected" in option(page, "base_1_article", f"set:{ids['extension']}")


def test_archived_inner_set_stays_in_the_form_marked(ready_client):  # decision 7
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, ids["basic"]).archived_at = ARCHIVED
    page = ready_client.get(f"/sets/{ids['complete']}/edit").text
    chosen = option(page, "base_0_article", f"set:{ids['basic']}")
    assert "selected" in chosen and en("catalog.archived_badge") in chosen
    assert option(page, "base_2_article", f"set:{ids['basic']}") is None or "disabled hidden" in option(page, "base_2_article", f"set:{ids['basic']}")


def test_bad_set_value_no_500(ready_client):
    a, ids = kits(ready_client)
    for value in ("set:x", "set:", f"set:{ids['basic']}:x", "set:99999999999999", "set:1:2:3:4"):
        response = ready_client.post("/sets/new", data=form(ready_client, base_0_article=value, base_1_article=str(a["Lid"])))
        assert response.status_code == 200, value
    response = ready_client.post("/sets/new", data=form(ready_client, base_0_article="set:9999", base_1_article=str(a["Lid"])))
    assert response.status_code == 200 and en("sets.not_found") in response.text


def test_breaks_parent_message_on_edit(ready_client):  # Spec 5.4 "Änderungen", condition A1
    a, ids = kits(ready_client)
    url = f"/sets/{ids['basic']}/edit"
    data = form(ready_client, url, name="Basic kit", sku="EX-GA", base_0_qty="2", base_0_article=str(a["Holder"]),
                base_1_article=str(a["Manual"]), base_2_article=str(a["Plate"]))
    response = ready_client.post(url, data=data)
    assert response.status_code == 200
    assert en("sets.breaks_parent", names="Complete kit", name="Plate") in response.text
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, ids["complete"]).archived_at = ARCHIVED
    response = ready_client.post(url, data=form(ready_client, url, name="Basic kit", sku="EX-GA", base_0_qty="2",
                                                base_0_article=str(a["Holder"]), base_1_article=str(a["Manual"]),
                                                base_2_article=str(a["Plate"])))
    assert en("sets.breaks_parent", names="Complete kit (archived)", name="Plate") in response.text


# ---- the set list and its tiles (Spec 10.1) -------------------------------------------------

SET_BADGE = '<span class="badge tone-neutral">{}</span>'


def test_list_and_tile_show_inner_set_with_badge_and_variant_number(ready_client):
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        make_set(s, "Bundle", [(1, inner(ids["sizes"], a["Small"])), (2, inner(ids["extension"]))])
    page = ready_client.get("/sets").text
    badge = SET_BADGE.format(en("sets.badge_set"))
    assert page.count(f"1 × Sizes EX-SZ-S {badge}") == 2  # list and tile
    assert page.count(f"2 × Extension {badge}") == 2
    assert page.count(f"1 × Basic kit EX-GA {badge}") == 0  # no choices: no variant number
    assert page.count(f"1 × Basic kit {badge}") == 2


def test_list_marks_archived_inner_set(ready_client):  # Spec 5.5
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, ids["basic"]).archived_at = ARCHIVED
    page = ready_client.get("/sets").text
    assert f'1 × Basic kit {SET_BADGE.format(en("sets.badge_set"))}{SET_BADGE.format(en("catalog.archived_badge"))}' in page


def test_set_with_incomplete_inner_set_shows_faulty(ready_client):  # decision 6 (condition A2 withdrawn)
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        lonely = make_set(s, "Lonely", [(1, a["Lid"])]).id
        make_set(s, "Outer", [(1, inner(lonely)), (1, a["Frame"])])
    page = ready_client.get("/sets").text
    assert f'1 × Lonely {SET_BADGE.format(en("sets.badge_set"))}</span>' in page
    assert SET_BADGE.format(en("sets.incomplete.broken")) in page


def test_broken_set_shows_its_badge(ready_client):  # decision 6
    a, ids = kits(ready_client)
    with sm_of(ready_client).begin() as s:
        from reprintbuddy.models import SetPosition
        position = SetPosition(set_id=ids["basic"], sort=5, qty=1, slot=0)
        s.add(position)
        s.flush()
        s.add(SetPositionSet(position_id=position.id, set_id=ids["complete"]))
    page = ready_client.get("/sets").text
    assert SET_BADGE.format(en("sets.incomplete.broken")) in page


def test_search_finds_the_outer_set(ready_client):
    a, ids = kits(ready_client)
    page = ready_client.get("/sets?q=EX-KP").text
    assert "Complete kit" in page and "Sizes" not in page.split('class="tiles"')[-1]


# ---- Altfall warning (Spec 5.4, 0.4.3) ------------------------------------------------------


def altfall(client, ids, a):
    with sm_of(client).begin() as s:
        from reprintbuddy.models import Article
        s.get(Article, a["Manual"]).archived_at = ARCHIVED  # an earlier version could leave it so


def test_set_list_warns_about_an_archived_article(ready_client):
    a, ids = kits(ready_client)
    altfall(ready_client, ids, a)
    page = ready_client.get("/sets").text
    text = en("sets.holds_archived_article", set="Basic kit", name="Manual")
    assert page.count(text) == 2  # list and tile
    assert en("sets.holds_archived_article", set="Complete kit", name="Manual") not in page  # only where it is fixed


def test_overview_warns_and_links_to_the_set(ready_client):
    a, ids = kits(ready_client)
    altfall(ready_client, ids, a)
    page = ready_client.get("/").text
    warnings = re.search(r"<!--live:warnings-->(.*?)<!--/live:warnings-->", page, re.S).group(1)
    assert en("sets.holds_archived_article", set="Basic kit", name="Manual") in warnings
    assert f'href="/sets/{ids["basic"]}/edit">{en("warning.action.set")}</a>' in warnings
    assert 'data-live-region="warnings" data-live-topics="bambuddy queue article settings notify set"' in page


def test_queue_page_does_not_repeat_the_set_warning(ready_client):
    a, ids = kits(ready_client)
    altfall(ready_client, ids, a)
    assert en("sets.holds_archived_article", set="Basic kit", name="Manual") not in ready_client.get("/queue").text
