"""Article pages and the automation (Spec 5.1, 7.1, 10.1; plan B4 Task 13 with its addendum).

The list shows "N in Bambuddy" and the grey "paused" badge; the status uses the full coverage.
The detail page shows in progress, queued and coverage, the entries and open prints of the
article, and the pause with the one way out that fits its reason.
"""

import re

from markupsafe import escape
from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.bambuddy.types import FilamentRequirement
from reprintbuddy.domain import catalog
from reprintbuddy.models import Article, ArticleFilament, JournalEntry
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate, translate_count


def en(key, **params):
    return str(escape(translate("en", key, **params)))


def de(key, **params):
    return str(escape(translate("de", key, **params)))


def stat(html, label):
    """The number shown next to a label on the detail page."""
    match = re.search(rf'{re.escape(label)}</span>\s*<span class="stat-value[^"]*">(-?\d+)<', html)
    assert match, f"no number for {label}"
    return match.group(1)


def article(client, name="Template A", **fields) -> int:
    with sm_of(client).begin() as s:
        return make_article(s, name=name, **fields).id


def entry(client, article_id, prints=1, **fields) -> int:
    with sm_of(client).begin() as s:
        return make_entry(s, article_id, prints, **fields).id


def dispatch(client, article_id, **fields) -> int:
    with sm_of(client).begin() as s:
        return make_dispatch(s, article_id, **fields).id


def reload(client, article_id) -> Article:
    with sm_of(client)() as s:
        found = s.get(Article, article_id)
        s.expunge(found)
        return found


def german(client):
    with sm_of(client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)


def post(client, page, action, **extra):
    data = {"csrf_token": csrf_of(client, page), "form_token": form_token_of(client, page, action), **extra}
    return client.post(action, data=data)


def table_part(page):
    return re.search(r"<table>.*?</table>", page, re.S).group(0)


def tiles_part(page):
    return re.search(r'<div class="tiles">.*', page, re.S).group(0)


# ---- list -----------------------------------------------------------------------------------


def test_list_shows_in_bambuddy(ready_client):  # Spec 10.1: "Nachdruck läuft" is a note, not a colour
    a = article(ready_client, units_per_print=2, stock=5, min_stock=1)
    article(ready_client, name="Template B", stock=5, min_stock=1)
    dispatch(ready_client, a, status="printing", units=2)
    page = ready_client.get("/articles").text
    note = en("tile.in_bambuddy", n=1)
    assert table_part(page).count(note) == 1  # under the badge in the list
    assert tiles_part(page).count(note) == 1  # in the tile footer
    assert re.search(rf'class="tile-foot">.*?{re.escape(note)}', tiles_part(page), re.S)
    # no new column: the list keeps article, status, stock, available, target and the
    # actions column of the new look
    assert len(re.findall(r"<th[ >]", table_part(page))) == 6
    german(ready_client)
    assert de("tile.in_bambuddy", n=1) in ready_client.get("/articles").text


def test_list_finished_prints_are_not_in_bambuddy(ready_client):
    a = article(ready_client, stock=5)
    dispatch(ready_client, a, status="completed")
    page = ready_client.get("/articles").text
    assert en("tile.in_bambuddy", n=1) not in page
    assert page.count('class="tile-foot"') == 0


def test_status_uses_full_coverage(ready_client):  # Spec 10.1 with coverage after Spec 7.1
    a = article(ready_client, stock=0, min_stock=1)
    entry(ready_client, a, prints=1)
    page = ready_client.get("/articles").text
    assert en("status.article.low") in page and en("status.article.below_min") not in page
    with sm_of(ready_client)() as s:
        row = catalog.list_articles(s)[0]
        assert (row.coverage, row.status, row.queued_units) == (1, "low", 1)
    detail = ready_client.get(f"/articles/{a}").text
    assert en("status.article.low") in detail and en("status.article.below_min") not in detail


def test_status_counts_open_prints(ready_client):
    a = article(ready_client, stock=0, min_stock=2, units_per_print=2)
    dispatch(ready_client, a, status="queued", units=2)
    with sm_of(ready_client)() as s:
        row = catalog.list_articles(s)[0]
        assert (row.in_progress_units, row.in_progress_prints, row.coverage, row.status) == (2, 1, 2, "low")


def test_paused_badge(ready_client):  # Spec 10.1: grey badge "paused"
    a = article(ready_client, stock=5, paused=True, pause_reason="two_failures")
    article(ready_client, name="Template B", stock=5)
    badge = f'<span class="badge tone-neutral">{en("status.paused")}</span>'
    page = ready_client.get("/articles").text
    assert table_part(page).count(badge) == 1 and tiles_part(page).count(badge) == 1
    assert badge in ready_client.get(f"/articles/{a}").text


# ---- detail ---------------------------------------------------------------------------------


def test_detail_coverage_and_entries(ready_client):
    a = article(ready_client, stock=3, reserved=1, provisional=1, min_stock=4)
    entry(ready_client, a, prints=2, origin="manual")
    dispatch(ready_client, a, status="queued", units=1, bambuddy_item_id=77)
    page = ready_client.get(f"/articles/{a}").text
    assert stat(page, en("articles.in_progress")) == "1"
    assert stat(page, en("articles.queued")) == "2"
    assert stat(page, en("articles.coverage")) == str(2 + 2 + 1 + 1)  # available + queued + in progress + provisional
    work = regions.extract(page, ["main"])["main"]
    assert translate_count("en", "queue.prints", 2) in work and en("queue.origin.manual") in work
    assert en("dispatch.status.queued") in work and ">77<" in work
    assert 'href="/queue#manual"' in work and en("queue.manual") in work


def test_detail_shows_nothing_running(ready_client):
    a = article(ready_client)
    dispatch(ready_client, a, status="completed")
    page = ready_client.get(f"/articles/{a}").text
    assert en("articles.nothing_running") in page
    assert en("dispatch.status.completed") not in page


def test_resume_button(ready_client):
    a = article(ready_client, paused=True, pause_reason="queue_reduced", consecutive_failures=1)
    page = ready_client.get(f"/articles/{a}").text
    assert en("pause.reason.queue_reduced") in page
    assert f'action="/articles/{a}/resume"' in page and en("articles.resume") in page
    action = f"/articles/{a}/resume"
    data = {"csrf_token": csrf_of(ready_client, f"/articles/{a}"),
            "form_token": form_token_of(ready_client, f"/articles/{a}", action)}
    done = ready_client.post(action, data=data)
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{a}"
    assert en("articles.resumed", name="Template A") in ready_client.get(f"/articles/{a}").text
    art = reload(ready_client, a)
    assert (art.paused, art.pause_reason, art.consecutive_failures) == (False, None, 0)
    with sm_of(ready_client)() as s:
        kinds = s.scalars(select(JournalEntry.kind).where(JournalEntry.article_id == a)).all()
        assert "automation_resumed" in kinds
    again = ready_client.post(action, data=data)
    assert again.status_code == 303
    assert en("form.duplicate") in ready_client.get(f"/articles/{a}").text


def test_resume_refused_shows_message(ready_client):
    a = article(ready_client)
    page = f"/articles/{a}"
    data = {"csrf_token": csrf_of(ready_client, page), "form_token": form_token_of(ready_client, page)}
    response = ready_client.post(f"/articles/{a}/resume", data=data)
    assert response.status_code == 200 and en("queue.not_paused") in response.text


def test_resume_needs_csrf(ready_client):
    a = article(ready_client, paused=True, pause_reason="queue_reduced")
    assert ready_client.post(f"/articles/{a}/resume", data={"form_token": "x"}).status_code == 403
    assert ready_client.post(f"/articles/{a}/confirm-file", data={"form_token": "x"}).status_code == 403
    assert reload(ready_client, a).paused is True


def test_no_target_needs_a_target(ready_client):
    a = article(ready_client, paused=True, pause_reason="no_target", target_model=None, printer_id=None)
    page = ready_client.get(f"/articles/{a}").text
    assert f'action="/articles/{a}/resume"' not in page
    assert en("articles.pause_needs_target") in page and f'href="/articles/{a}/edit"' in page
    with sm_of(ready_client).begin() as s:
        s.get(Article, a).target_model = "A1"
    page = ready_client.get(f"/articles/{a}").text
    assert f'action="/articles/{a}/resume"' in page
    assert post(ready_client, f"/articles/{a}", f"/articles/{a}/resume").status_code == 303
    assert reload(ready_client, a).paused is False


def test_file_changed_offers_confirm(bb_client, fake_bambuddy):
    a = article(bb_client, library_file_id=1, plate_id=1, file_name="Template A.gcode.3mf",
                file_fingerprint="v1", paused=True, pause_reason="file_changed")
    fake_bambuddy.update_file(1, fingerprint="v2")
    fake_bambuddy.set_requirements(1, 1, [FilamentRequirement(1, 50.0, "PETG", "#FFFFFF")])
    page = bb_client.get(f"/articles/{a}").text
    assert en("pause.reason.file_changed") in page
    assert f'action="/articles/{a}/resume"' not in page
    assert f'action="/articles/{a}/confirm-file"' in page and en("articles.confirm_file") in page
    assert f'href="/articles/{a}/relink"' in page  # choosing again is the other way out
    done = post(bb_client, f"/articles/{a}", f"/articles/{a}/confirm-file")
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{a}"
    assert en("articles.file_confirmed", name="Template A") in bb_client.get(f"/articles/{a}").text
    art = reload(bb_client, a)
    assert (art.file_fingerprint, art.paused, art.pause_reason) == ("v2", False, None)
    with sm_of(bb_client)() as s:
        rows = s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == a)).all()
        assert [(r.material, r.grams) for r in rows] == [("PETG", 50.0)]


def test_confirm_file_gone_or_unreachable(bb_client, fake_bambuddy):
    a = article(bb_client, library_file_id=1, plate_id=1, file_fingerprint="v1",
                paused=True, pause_reason="file_changed")
    page = f"/articles/{a}"
    action = f"/articles/{a}/confirm-file"
    data = {"csrf_token": csrf_of(bb_client, page), "form_token": form_token_of(bb_client, page, action)}
    fake_bambuddy.fail_next("get_file", BambuddyUnavailable("down"))
    failed = bb_client.post(action, data=data)
    assert failed.status_code == 200 and en("articles.bambuddy_error") in failed.text
    fake_bambuddy.remove_file(1)
    gone = bb_client.post(action, data=data)  # the token is still unused
    assert gone.status_code == 200 and en("articles.file_missing") in gone.text
    art = reload(bb_client, a)
    assert (art.file_fingerprint, art.paused) == ("v1", True)


def test_confirm_file_without_bambuddy(ready_client):
    a = article(ready_client, paused=True, pause_reason="file_changed")
    response = post(ready_client, f"/articles/{a}", f"/articles/{a}/confirm-file")
    assert response.status_code == 200 and en("articles.bambuddy_missing") in response.text
    assert reload(ready_client, a).paused is True


def test_file_missing_offers_relink(bb_client, fake_bambuddy):
    a = article(bb_client, library_file_id=1, plate_id=1, paused=True, pause_reason="file_missing")
    fake_bambuddy.remove_file(1)
    page = bb_client.get(f"/articles/{a}").text
    assert en("pause.reason.file_missing") in page
    assert f'href="/articles/{a}/relink"' in page and en("articles.choose_file_again") in page
    assert f'action="/articles/{a}/resume"' not in page and f'action="/articles/{a}/confirm-file"' not in page
    browse = bb_client.get(f"/articles/{a}/relink?folder=1")
    assert browse.status_code == 200 and f"/articles/{a}/relink/file/2" in browse.text
    plates = bb_client.get(f"/articles/{a}/relink/file/2")
    assert plates.status_code == 200 and en("articles.use_plate") in plates.text
    assert en("articles.restore_with_plate") not in plates.text  # the article is not archived
    done = post(bb_client, f"/articles/{a}/relink/file/2", f"/articles/{a}/relink/file/2/plate/2")
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{a}"
    assert en("articles.relinked", name="Template A") in bb_client.get(f"/articles/{a}").text
    art = reload(bb_client, a)
    assert (art.archived_at, art.library_file_id, art.plate_id) == (None, 2, 2)
    assert (art.paused, art.pause_reason) == (False, None)
    with sm_of(bb_client)() as s:
        kinds = s.scalars(select(JournalEntry.kind).where(JournalEntry.article_id == a)).all()
        assert "article_relinked" in kinds


def test_relink_active_plate_taken(bb_client):
    a = article(bb_client, library_file_id=1, plate_id=1, paused=True, pause_reason="file_missing")
    article(bb_client, name="Template B", library_file_id=2, plate_id=2)
    page = f"/articles/{a}/relink/file/2"
    # plate 2 is taken, so its form is missing: the tokens come from the form of plate 1
    data ={"csrf_token": csrf_of(bb_client, page), "form_token": form_token_of(bb_client, page, f"{page}/plate/1")}
    taken = bb_client.post(f"{page}/plate/2", data=data)
    assert taken.status_code == 200 and en("articles.plate_taken", name="Template B") in taken.text
    art = reload(bb_client, a)
    assert (art.library_file_id, art.plate_id, art.paused) == (1, 1, True)


def test_archive_blocked_messages(ready_client):  # Spec 5.5, plan B4 decision 20
    a = article(ready_client)
    entry(ready_client, a)
    response = post(ready_client, f"/articles/{a}", f"/articles/{a}/archive")
    assert response.status_code == 200 and en("articles.archive_in_queue") in response.text
    b = article(ready_client, name="Template B")
    dispatch(ready_client, b, status="printing")
    response = post(ready_client, f"/articles/{b}", f"/articles/{b}/archive")
    assert response.status_code == 200 and en("articles.archive_in_progress") in response.text
    assert reload(ready_client, a).archived_at is None and reload(ready_client, b).archived_at is None


def test_detail_without_bambuddy(ready_client):
    a = article(ready_client, paused=True, pause_reason="file_changed")
    entry(ready_client, a)
    dispatch(ready_client, a, status="unclear")
    assert ready_client.get(f"/articles/{a}").status_code == 200


def test_article_automation_german(ready_client):
    a = article(ready_client, paused=True, pause_reason="queue_reduced")
    b = article(ready_client, name="Template B", paused=True, pause_reason="file_changed")
    german(ready_client)
    page = ready_client.get(f"/articles/{a}").text
    assert "Pause aufheben" in page and "Deckung" in page and "In Arbeit" in page
    assert "Druckdatei bestätigen" in ready_client.get(f"/articles/{b}").text


def test_relink_offers_the_articles_own_plate(bb_client):
    """Final review 6: an active article may choose its own plate again (a new file version)."""
    a = article(bb_client, library_file_id=1, plate_id=1, paused=True, pause_reason="file_changed")
    page = bb_client.get(f"/articles/{a}/relink/file/1").text
    assert f'action="/articles/{a}/relink/file/1/plate/1"' in page
    assert esc_text(en("articles.plate_taken_by", name="Template A")) not in page


def esc_text(text):
    return str(escape(text))
