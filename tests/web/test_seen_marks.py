"""Loading /orders or /reorder marks what is seen for "new" on the overview (Spec 10.2, 0.4.1).

Only loading the page counts: the live refetch of its regions does not (a window left open on the PC
would otherwise clear every "new" on the phone), and neither does a time when writes are paused.
"""

from conftest import sm_of
from factories import make_order, make_part
from reprintbuddy import settings as st
from reprintbuddy.web import regions


def setting(client, key):
    with sm_of(client).begin() as s:
        return st.get_setting(s, key)


def test_loading_orders_marks_the_orders(ready_client):
    with sm_of(ready_client).begin() as s:
        last = make_order(s, reference="R2").id
    assert setting(ready_client, "orders_seen_id") is None
    assert ready_client.get("/orders").status_code == 200
    assert setting(ready_client, "orders_seen_id") == last


def test_a_live_refetch_of_orders_is_not_loading(ready_client):
    ready_client.get("/orders")
    with sm_of(ready_client).begin() as s:
        make_order(s, reference="R9")
    for name in ("summary", "list"):
        ready_client.get("/orders", headers={regions.HEADER: name})
    assert setting(ready_client, "orders_seen_id") == 0


def test_loading_reorder_marks_the_suggestions(ready_client):
    with sm_of(ready_client).begin() as s:
        part = make_part(s, name="Insert", stock=0, min_stock=10).id  # below its minimum: a suggestion
    ready_client.get("/reorder", headers={regions.HEADER: "suggestions"})
    assert setting(ready_client, "reorder_seen") is None
    ready_client.get("/reorder")
    assert setting(ready_client, "reorder_seen") == [f"part:{part}"]


def test_nothing_is_marked_while_writes_are_paused(ready_client, monkeypatch):
    with sm_of(ready_client).begin() as s:
        make_order(s)
        make_part(s, name="Insert", stock=0, min_stock=10)
    monkeypatch.setattr(ready_client.app.state.activity, "may_write", lambda: False)
    assert ready_client.get("/orders").status_code == 200
    assert ready_client.get("/reorder").status_code == 200
    assert setting(ready_client, "orders_seen_id") is None
    assert setting(ready_client, "reorder_seen") is None


def test_the_closed_orders_are_not_the_open_ones(ready_client):
    with sm_of(ready_client).begin() as s:
        make_order(s)
    ready_client.get("/orders?view=closed")
    assert setting(ready_client, "orders_seen_id") is None


def test_loading_orders_with_nothing_new_takes_no_write_lock(ready_client, monkeypatch):
    """Review v0.4.1 B, R1: the open orders used to only read; with the mark unchanged they still do
    (no BEGIN IMMEDIATE that would wait for the automation's writes)."""
    from reprintbuddy.web.routes import orders as route

    with sm_of(ready_client).begin() as s:
        make_order(s)
    ready_client.get("/orders")  # sets the mark once

    def no_lock(request):
        raise AssertionError("opened a write transaction with nothing to mark")

    monkeypatch.setattr(route, "write_transaction", no_lock)
    assert ready_client.get("/orders").status_code == 200


# ---- R4 (decided 11.11.2026): a reload by Reprintbuddy itself is no opening ---------------------
# live.js loads the page again by itself after an update or restart (new version), after a refetch
# that failed and after being signed out; before it does, it sets the short-lived cookie
# `regions.AUTO_RELOAD_COOKIE`, and the next page clears it. Only opening by you counts.


def auto_reloaded(client, path):
    client.cookies.set(regions.AUTO_RELOAD_COOKIE, "1")
    try:
        return client.get(path)
    finally:
        client.cookies.delete(regions.AUTO_RELOAD_COOKIE)


def test_an_automatic_reload_of_orders_is_no_opening(ready_client):
    with sm_of(ready_client).begin() as s:
        make_order(s)
    assert auto_reloaded(ready_client, "/orders").status_code == 200
    assert setting(ready_client, "orders_seen_id") is None
    ready_client.get("/orders")  # opened by you
    assert setting(ready_client, "orders_seen_id") is not None


def test_an_automatic_reload_of_reorder_is_no_opening(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Insert", stock=0, min_stock=10)
    assert auto_reloaded(ready_client, "/reorder").status_code == 200
    assert setting(ready_client, "reorder_seen") is None


def test_live_script_marks_each_automatic_reload_and_not_the_button():
    """The three automatic cases all go through `reloadOrNotice`, which marks the reload; the button
    of the notice ("Reload", your click) does not; every page clears a mark left over."""
    import re
    from pathlib import Path

    from reprintbuddy.web import i18n

    text = (Path(i18n.__file__).parent / "static" / "js" / "live.js").read_text(encoding="utf-8")
    assert regions.AUTO_RELOAD_COOKIE in text

    def body(name):
        found = re.search(rf"\n  function {name}\((.*?)\) \{{\n(.*?)\n  \}}\n", text, re.S)
        assert found, name
        return found.group(2)

    assert "reloadIfSafe(true)" in body("reloadOrNotice")
    assert "automatic" in body("reloadIfSafe") and "AUTO_RELOAD" in body("reloadIfSafe")
    for caller in ("onNewVersion",):  # new version after an update or restart
        assert "reloadOrNotice();" in body(caller)
    assert text.count("reloadOrNotice();") == 3  # also the failed refetch (or sign-in redirect), a lost region
    click = re.search(r'addEventListener\("click", function \(event\) \{(.*?)\n  \}\);', text, re.S)
    assert click and "reloadIfSafe()" in click.group(1) and "reloadIfSafe(true)" not in click.group(1)
    assert re.search(r"max-age=0", text)  # the next page clears the mark
