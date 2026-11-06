"""Named regions of a page, fetched by the page itself (Spec 10.5.3, plan decision 5)."""

import re

import pytest
from starlette.requests import Request

from conftest import csrf_of, form_token_of, sm_of
from factories import make_part
from reprintbuddy import __version__
from reprintbuddy.web import regions
from reprintbuddy.web.app import TEMPLATES_DIR
from reprintbuddy.web.i18n import translate

REGIONS = {regions.HEADER: "journal"}


def en(key, **params):
    return translate("en", key, **params)


def fake_request(method="GET", header=None):
    headers = [] if header is None else [(regions.HEADER.lower().encode(), header.encode())]
    return Request({"type": "http", "method": method, "path": "/", "headers": headers, "query_string": b""})


def part_receipt(client, **fields):
    """Post the part receipt form with fresh tokens; the redirect is not followed."""
    data = {
        "csrf_token": csrf_of(client, "/bookings"),
        "form_token": form_token_of(client, "/bookings", "/bookings/part-receipt"),
    }
    data.update(fields)
    return client.post("/bookings/part-receipt", data=data)


def insert(client):
    with sm_of(client).begin() as s:
        return make_part(s, name="Insert M4").id


def test_extract_cuts_named_regions():
    html = "<html><!--live:a--><div>A1</div><!--/live:a--> mid <!--live:b-->B &amp; more<!--/live:b--></html>"
    found = regions.extract(html, ["b", "a", "c"])
    assert list(found) == ["b", "a"]
    assert found == {"b": "B &amp; more", "a": "<div>A1</div>"}


def test_requested_parses_and_limits():
    assert regions.requested(fake_request(header="list  summary list BAD x/y")) == ["list", "summary"]
    ten = " ".join(f"r{n}" for n in range(10))
    assert regions.requested(fake_request(header=ten)) == [f"r{n}" for n in range(8)]
    assert regions.requested(fake_request(method="POST", header="list")) == []
    assert regions.requested(fake_request()) == []


def test_journal_page_marks_its_region(ready_client):
    page = ready_client.get("/journal").text
    assert "<!--live:journal-->" in page and "<!--/live:journal-->" in page
    assert 'data-live-region="journal"' in page and 'data-live-topics="journal"' in page


def test_page_names_its_live_source_and_state(ready_client):
    page = ready_client.get("/journal?article=3").text
    assert 'data-live-src="/journal?article=3"' in page
    assert f'data-live-state="{ready_client.app.state.live.state}"' in page
    assert f'data-live="/live?v={__version__}"' in page


def test_region_request_returns_only_the_region(ready_client):
    response = ready_client.get("/journal", headers=REGIONS)
    assert response.status_code == 200
    assert response.text.startswith('<div class="live-region" data-live-region="journal"')
    assert "<html" not in response.text and "sidebar" not in response.text
    assert response.headers["x-live-regions"] == "journal"
    assert response.headers["cache-control"] == "no-store"
    assert "X-Live-Regions" in response.headers["vary"]


def test_region_matches_the_same_part_of_the_page(ready_client):
    assert part_receipt(ready_client, part_id=insert(ready_client), qty="50").status_code == 303
    whole = ready_client.get("/journal").text
    piece = ready_client.get("/journal", headers=REGIONS).text
    same = re.compile(r'(name="form_token" value=")[^"]*"')
    whole, piece = same.sub(r'\1x"', whole), same.sub(r'\1x"', piece)
    assert "Insert M4" in piece
    assert piece == regions.extract(whole, ["journal"])["journal"]


def test_region_request_keeps_the_messages(ready_client):  # Review Focus 3: two tabs, one cookie
    assert part_receipt(ready_client, part_id=insert(ready_client), qty="50").status_code == 303
    assert ready_client.get("/journal", headers=REGIONS).status_code == 200  # the other tab
    page = ready_client.get("/bookings").text  # the tab that booked follows its redirect
    assert en("bookings.part_receipt_done", item="Insert M4", qty=50) in page


def queue_both_kinds_of_message(client):
    """One message in the queued list (flash.py) and one single notice (deps.flash).

    The tokens are fetched first: every page shown afterwards would use the messages up.
    """
    part_id = insert(client)
    csrf = csrf_of(client, "/bookings")
    token = form_token_of(client, "/bookings", "/bookings/part-receipt")
    receipt = {"csrf_token": csrf, "form_token": token, "part_id": part_id, "qty": "50"}
    assert client.post("/bookings/part-receipt", data=receipt).status_code == 303
    language = {"csrf_token": csrf, "language": "en"}
    assert client.post("/settings/language", data=language).status_code == 303


def assert_both_messages_shown(client):
    page = client.get("/settings").text  # shows the list and the single notice
    assert en("bookings.part_receipt_done", item="Insert M4", qty=50) in page
    assert en("settings.saved") in page


def test_region_request_for_an_error_page_keeps_the_messages(ready_client):  # Review Focus 3
    # The article was deleted in the meantime: the region request ends in a 404 page.
    queue_both_kinds_of_message(ready_client)
    response = ready_client.get("/articles/999999", headers=REGIONS)
    assert response.status_code == 404 and "x-live-regions" not in response.headers
    assert "set-cookie" not in response.headers
    assert_both_messages_shown(ready_client)


def test_region_request_does_not_touch_the_session(ready_client):
    # Messages of both kinds are waiting; a region request uses up neither.
    queue_both_kinds_of_message(ready_client)
    response = ready_client.get("/journal", headers=REGIONS)
    assert response.status_code == 200 and "x-live-regions" in response.headers
    assert "set-cookie" not in response.headers
    assert_both_messages_shown(ready_client)


def test_region_request_signed_out_brings_no_regions(ready_client):  # Review Focus 1
    ready_client.cookies.clear()
    response = ready_client.get("/journal", headers=REGIONS)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    login = ready_client.get("/journal", headers=REGIONS, follow_redirects=True)
    assert login.status_code == 200 and "<html" in login.text
    assert "x-live-regions" not in login.headers


def test_unknown_region_gets_the_whole_page(ready_client):
    response = ready_client.get("/journal", headers={regions.HEADER: "nosuch"})
    assert response.status_code == 200 and "<html" in response.text
    assert "x-live-regions" not in response.headers


def test_post_rendered_page_has_no_region_source(ready_client):  # Review Focus 2
    response = part_receipt(ready_client, part_id=insert(ready_client), qty="0")
    assert response.status_code == 200
    assert 'data-live-src=""' in response.text


def test_region_names_are_valid_and_unique_per_template():
    call = re.compile(r"""ui\.region\(\s*["']([^"']*)["']""")
    found = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        if path.name == "_ui.html":  # defines the macro; its comment shows an example call
            continue
        names = call.findall(path.read_text(encoding="utf-8"))
        if names:
            found[path.name] = names
        for name in names:
            assert regions.NAME.fullmatch(name), (path.name, name)
        assert len(names) == len(set(names)), (path.name, names)
    assert found["journal.html"] == ["journal"]


# Pages with regions, as the signed-in browser sees them. Add each page that gets regions.
PAGES_WITH_REGIONS = ["/journal", "/articles", "/orders", "/orders?view=closed", "/orders/new"]
_MARK = re.compile(r"<!--(/?)live:([^>]*?)-->")


@pytest.mark.parametrize("path", PAGES_WITH_REGIONS)
def test_rendered_regions_are_balanced_unnested_and_unique(ready_client, path):
    page = ready_client.get(path)
    assert page.status_code == 200, path
    open_name, seen = None, []
    for closing, name in _MARK.findall(page.text):
        if not closing:
            assert open_name is None, f"{path}: region {name!r} inside region {open_name!r}"
            assert name not in seen, f"{path}: region {name!r} twice"
            assert regions.NAME.fullmatch(name), (path, name)
            open_name = name
            seen.append(name)
        else:
            assert name == open_name, f"{path}: end of {name!r} while {open_name!r} is open"
            open_name = None
    assert open_name is None, f"{path}: region {open_name!r} never ends"
    assert seen, f"{path}: no region"
