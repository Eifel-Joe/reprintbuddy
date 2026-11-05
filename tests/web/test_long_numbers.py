"""Very long digit strings (Python 3.12 raises ValueError above 4300 digits) never give a 500."""

import pytest

from conftest import csrf_of, sm_of
from factories import make_article
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import PositionInput, SetInput

LONG = "9" * 5000


@pytest.fixture
def seeded(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B", library_file_id=2).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a, b])])).id
        position = catalog.list_sets(s)[0].positions[0].id
    return bb_client, a, set_id, position


def order_data(client, **over):
    data = {
        "csrf_token": csrf_of(client, "/orders/new"), "form_token": "x",
        "reference": "Order 1001", "order_date": "2026-01-15", "note": "", "rows": "5",
    }
    data.update(over)
    return data


def test_order_form_fields(seeded):
    client, a, set_id, position = seeded
    for over in (
        {"rows": LONG},
        {"item_0": f"article:{LONG}", "qty_0": "1"},
        {"item_0": f"set:{LONG}", "qty_0": "1"},
        {"item_0": f"article:{a}", "qty_0": LONG},
        {"item_0": f"set:{set_id}", "qty_0": "1", f"choice_0_{position}": LONG},
    ):
        response = client.post("/orders/new", data=order_data(client, **over))
        assert response.status_code in (200, 400), list(over)


def test_set_form_fields(seeded):
    client, a, *_ = seeded
    for over in ({"rows": LONG}, {"pos_0_articles": [LONG]}, {"pos_0_qty": LONG}):
        data = {"csrf_token": csrf_of(client, "/sets/new"), "form_token": "x", "name": "S", "rows": "5",
                "pos_0_qty": "1", "pos_0_articles": [str(a)]}
        data.update(over)
        assert client.post("/sets/new", data=data).status_code in (200, 400), list(over)


def test_query_parameters(seeded):
    client, *_ = seeded
    for path in (f"/journal?article={LONG}", f"/journal?part={LONG}", f"/journal?page={LONG}",
                 f"/articles/new?folder={LONG}", f"/articles/1/relink?folder={LONG}"):
        assert client.get(path).status_code in (200, 404), path


@pytest.mark.parametrize("path", [
    "/orders/{}", "/articles/{}", "/articles/{}/edit", "/articles/{}/thumbnail", "/articles/{}/relink",
    "/articles/{}/relink/file/{}", "/articles/new/file/{}", "/articles/new/file/{}/plate/1",
    "/articles/new/file/1/plate/{}", "/articles/new/file/1/plate/{}/thumbnail",
    "/parts/{}/edit", "/sets/{}/edit",
])
def test_paths_with_long_numbers_are_404(seeded, path):
    client, *_ = seeded
    assert client.get(path.format(LONG, LONG)).status_code == 404


@pytest.mark.parametrize("path", [
    "/orders/{}/ship", "/orders/{}/cancel", "/articles/{}/archive", "/articles/{}/restore",
    "/parts/{}/archive", "/sets/{}/restore", "/articles/{}/relink/file/1/plate/1",
    "/articles/new/file/{}/plate/1", "/articles/{}/edit", "/parts/{}/edit", "/sets/{}/edit",
])
def test_post_paths_with_long_numbers_are_404(seeded, path):
    client, *_ = seeded
    data = {"csrf_token": csrf_of(client, "/orders"), "form_token": "x"}
    assert client.post(path.format(LONG), data=data).status_code == 404
