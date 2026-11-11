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
