"""Reading the open queue and finding entries by batch (B2 Task 5, Decisions 13 and 14)."""

import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import BambuddyUnexpected
from reprintbuddy.bambuddy.parse import parse_folders, parse_queue_items, parse_spools
from reprintbuddy.bambuddy.types import QueueItem


def _query(route) -> str:
    return route.calls.last.request.url.query.decode()


async def test_list_queue_asks_for_open_items(respx_mock):
    route = respx_mock.get(f"{API}/queue/", params={"status": "pending,printing"}).respond(
        200, json=load("queue_open.json")
    )
    items = await client().list_queue()
    assert [i.id for i in items] == [e["id"] for e in load("queue_open.json")]
    assert all(isinstance(i, QueueItem) for i in items)
    assert route.calls.last.request.url.path == "/api/v1/queue/"
    assert list(route.calls.last.request.url.params.multi_items()) == [("status", "pending,printing")]


def _entry(item_id: int, status: str, batch_id: int | None) -> dict:
    entry = dict(load("queue_all.json")[0])
    entry.update(id=item_id, status=status, batch_id=batch_id)
    return entry


async def test_find_items_by_batch_filters(respx_mock):
    data = load("queue_all.json") + [_entry(901, "completed", 42), _entry(902, "pending", 42)]
    route = respx_mock.get(f"{API}/queue/").respond(200, json=data)
    items = await client().find_items_by_batch(42)
    assert [(i.id, i.status) for i in items] == [(901, "completed"), (902, "pending")]
    assert _query(route) == ""  # no filter: the history is part of the answer
    assert await client().find_items_by_batch(43) == []


async def test_unknown_status_passed_raw(respx_mock):
    data = load("queue_open.json") + [_entry(903, "paused", 7)]
    respx_mock.get(f"{API}/queue/", params={"status": "pending,printing"}).respond(200, json=data)
    items = await client().list_queue()
    assert len(items) == len(data)
    assert items[-1].status == "paused"
    assert [i.id for i in items[:-1]] == [e["id"] for e in load("queue_open.json")]


async def test_queue_answer_not_a_list(respx_mock):
    respx_mock.get(f"{API}/queue/").respond(200, json={"items": []})
    with pytest.raises(BambuddyUnexpected):
        await client().find_items_by_batch(1)
    with pytest.raises(BambuddyUnexpected):
        await client().list_queue()


def test_parse_queue_items_rejects_half_entry():
    with pytest.raises(BambuddyUnexpected):
        parse_queue_items([{"id": 1}])


# ---- hardenings (addendum) -------------------------------------------------------------


async def test_json_recursion_is_unexpected(respx_mock):
    deep = "[" * 200_000 + "]" * 200_000
    respx_mock.get(f"{API}/queue/").respond(200, content=deep, headers={"content-type": "application/json"})
    with pytest.raises(BambuddyUnexpected):
        await client().find_items_by_batch(1)


def test_parse_folders_recursion_is_unexpected():
    tree: dict = {"id": 0, "name": "Templates", "children": []}
    node = tree
    for n in range(1, 5000):
        child = {"id": n, "name": "Templates", "children": []}
        node["children"].append(child)
        node = child
    with pytest.raises(BambuddyUnexpected):
        parse_folders([tree])


@pytest.mark.parametrize("value", [1000.5, 1000.0, float("inf"), float("nan"), True, "1000"])
def test_label_weight_must_be_int(value):
    data = load("spools_builtin.json")
    data[0]["label_weight"] = value
    with pytest.raises(BambuddyUnexpected):
        parse_spools(data)


def test_label_weight_int_still_works():
    data = load("spools_builtin.json")
    data[0]["label_weight"] = 1000
    assert parse_spools(data)[0].label_g == 1000
