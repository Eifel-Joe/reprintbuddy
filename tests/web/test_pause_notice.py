"""The notice of a paused article says how to go on (p49).

Overview and queue show the notice in the live region "warnings": a button that lifts the
pause (and returns to the page it was on), or - where lifting is refused anyway - the link to
the place where the pause is settled, plus always a link to the article.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from texts import en_html as en
from reprintbuddy import settings as st
from reprintbuddy.models import Article
from reprintbuddy.web import regions


def article(client, name="Template A", **fields) -> int:
    with sm_of(client).begin() as s:
        return make_article(s, name=name, **fields).id


def paused_of(client, article_id) -> bool:
    with sm_of(client)() as s:
        return s.get(Article, article_id).paused


def notice(page: str) -> str:
    found = regions.extract(page, ["warnings"])
    assert "warnings" in found
    return found["warnings"]


def card_in(region_html: str, text: str) -> str:
    """The one warning card of the region that contains `text`."""
    cards = re.split(r'(?=<div class="warning-card)', region_html)[1:]
    mine = [c for c in cards if text in c]
    assert len(mine) == 1, (text, cards)
    return mine[0]


def card_of(page: str, name: str) -> str:
    return card_in(notice(page), name)


PAGES = ["/", "/queue"]


@pytest.mark.parametrize("page", PAGES)
def test_liftable_pause_has_button_and_article_link(ready_client, page):
    a = article(ready_client, paused=True, pause_reason="missing_in_bambuddy", target_model="A1")
    card = card_of(ready_client.get(page).text, "Template A")
    assert f'action="/articles/{a}/resume"' in card
    assert en("warning.action.resume") in card
    assert f'name="next" value="{page}"' in card
    assert 'name="csrf_token"' in card and 'name="form_token"' in card
    assert f'href="/articles/{a}"' in card and en("warning.action.article") in card
    assert "/confirm-file" not in card and "/relink" not in card


@pytest.mark.parametrize("page", PAGES)
def test_file_changed_links_to_confirm_without_button(ready_client, page):
    a = article(ready_client, paused=True, pause_reason="file_changed")
    card = card_of(ready_client.get(page).text, "Template A")
    assert f'href="/articles/{a}#confirm-file"' in card
    assert en("warning.action.confirm_file") in card
    assert "<button" not in card and "/resume" not in card
    assert f'href="/articles/{a}"' in card


@pytest.mark.parametrize("page", PAGES)
def test_file_missing_links_to_relink_without_button(ready_client, page):
    a = article(ready_client, paused=True, pause_reason="file_missing")
    card = card_of(ready_client.get(page).text, "Template A")
    assert f'href="/articles/{a}/relink"' in card
    assert en("warning.action.relink") in card
    assert "<button" not in card and "/resume" not in card


def test_no_target_without_target_links_to_edit(ready_client):
    """Lifting would be refused (articles.pause_needs_target): the way is the edit page."""
    a = article(ready_client, paused=True, pause_reason="no_target", target_model=None, printer_id=None)
    card = card_of(ready_client.get("/").text, "Template A")
    assert f'href="/articles/{a}/edit"' in card and en("warning.action.edit") in card
    assert "<button" not in card and "/resume" not in card


def test_no_target_with_target_can_be_lifted(ready_client):
    a = article(ready_client, paused=True, pause_reason="no_target", target_model="A1")
    assert f'action="/articles/{a}/resume"' in card_of(ready_client.get("/").text, "Template A")


def test_pause_without_reason_can_be_lifted(ready_client):
    a = article(ready_client, paused=True, pause_reason=None, target_model="A1")
    assert f'action="/articles/{a}/resume"' in card_of(ready_client.get("/").text, "Template A")


def test_other_warnings_have_no_actions(ready_client):
    with sm_of(ready_client)() as s:
        st.set_setting(s, "queue_running", False)
    card = card_of(ready_client.get("/").text, en("warning.queue_stopped"))
    assert "<a " not in card and "<form" not in card


def test_text_stays_name_and_reason(ready_client):
    article(ready_client, paused=True, pause_reason="two_failures", target_model="A1")
    card = card_of(ready_client.get("/").text, "Template A")
    assert f"<p>Template A: {en('pause.reason.two_failures')}</p>" in card


def test_confirm_anchor_exists_on_the_article_page(ready_client):
    a = article(ready_client, paused=True, pause_reason="file_changed")
    assert 'id="confirm-file"' in ready_client.get(f"/articles/{a}").text


# ---- the button ----------------------------------------------------------------------------------


def lift(client, a, page, **extra):
    action = f"/articles/{a}/resume"
    data = {
        "csrf_token": csrf_of(client, page),
        "form_token": form_token_of(client, page, action),
        "next": page,
        **extra,
    }
    return client.post(action, data=data)


@pytest.mark.parametrize("page", PAGES)
def test_button_lifts_and_returns_to_the_page(ready_client, page):
    a = article(ready_client, paused=True, pause_reason="missing_in_bambuddy", target_model="A1")
    done = lift(ready_client, a, page)
    assert done.status_code == 303 and done.headers["location"] == page
    assert paused_of(ready_client, a) is False
    assert en("articles.resumed", name="Template A") in ready_client.get(page).text


@pytest.mark.parametrize("bad", ["//evil", "https://evil", "\\\\evil", "", "evil"])
def test_foreign_next_goes_to_the_article(ready_client, bad):
    a = article(ready_client, paused=True, pause_reason="missing_in_bambuddy", target_model="A1")
    done = lift(ready_client, a, "/queue", next=bad)
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{a}"
    assert paused_of(ready_client, a) is False


def test_button_without_csrf_is_refused(ready_client):
    a = article(ready_client, paused=True, pause_reason="missing_in_bambuddy", target_model="A1")
    response = ready_client.post(
        f"/articles/{a}/resume",
        data={"form_token": form_token_of(ready_client, "/", f"/articles/{a}/resume"), "next": "/"},
    )
    assert response.status_code == 403
    assert paused_of(ready_client, a) is True


def test_refusal_shows_its_message(ready_client):
    """lift_pause refuses (the target went away meanwhile): the answer shows the message."""
    a = article(ready_client, paused=True, pause_reason="no_target", target_model="A1")
    token = form_token_of(ready_client, "/", f"/articles/{a}/resume")
    csrf = csrf_of(ready_client, "/")
    with sm_of(ready_client).begin() as s:
        art = s.get(Article, a)
        art.target_model = None
        art.printer_id = None
    response = ready_client.post(
        f"/articles/{a}/resume", data={"csrf_token": csrf, "form_token": token, "next": "/"}
    )
    page = response if response.status_code == 200 else ready_client.get(response.headers["location"])
    assert en("articles.pause_needs_target") in page.text
    assert paused_of(ready_client, a) is True


def test_blocked_while_an_update_runs(ops_env):
    from reprintbuddy.ops.status import UpdateStatus, write_status
    from test_ops_update import data_dir

    client = ops_env.client
    a = article(client, paused=True, pause_reason="missing_in_bambuddy", target_model="A1")
    token = csrf_of(client, "/")
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=started))
    response = client.post(f"/articles/{a}/resume", data={"csrf_token": token, "next": "/"})
    assert response.status_code == 503
    assert en("ops.update.in_progress") in response.text
    assert paused_of(client, a) is True


# ---- the live region -------------------------------------------------------------------------------


@pytest.mark.parametrize("page", PAGES)
def test_live_fragment_has_a_working_button(ready_client, page):
    a = article(ready_client, paused=True, pause_reason="missing_in_bambuddy", target_model="A1")
    csrf = csrf_of(ready_client, page)
    fragment = ready_client.get(page, headers={regions.HEADER: "warnings"})
    assert fragment.status_code == 200
    card = card_in(fragment.text, "Template A")
    assert f'name="next" value="{page}"' in card
    token = re.search(r'name="form_token" value="([^"]+)"', card).group(1)
    done = ready_client.post(
        f"/articles/{a}/resume", data={"csrf_token": csrf, "form_token": token, "next": page}
    )
    assert done.status_code == 303 and done.headers["location"] == page
    assert paused_of(ready_client, a) is False
