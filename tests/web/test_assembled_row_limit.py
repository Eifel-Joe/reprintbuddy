"""An article made of parts with as many rows as the form allows (p391, Spec 5.6)."""

from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_article, make_assembled
from reprintbuddy.models import ArticleComponent
from reprintbuddy.web.routes.assembled import MAX_ROWS


def _box_with_max_rows(client) -> tuple[int, list[int]]:
    with sm_of(client).begin() as s:
        ids = [make_article(s, name=f"Panel {i:03d}").id for i in range(MAX_ROWS)]
        box = make_assembled(s, name="Big box", base=[(i, 1) for i in ids]).id
    return box, ids


def test_saving_with_one_blank_row_more_than_the_limit_keeps_every_row(ready_client):
    """The edit form shows one empty row below the saved ones; at the limit that is row 201. The server
    takes the rows up to the limit instead of falling back to three rows and dropping the rest."""
    box, ids = _box_with_max_rows(ready_client)
    data = {
        "csrf_token": csrf_of(ready_client, f"/articles/{box}/assembled"),
        "name": "Big box", "sku": "EX-BIG", "mode": "stock", "base_rows": str(MAX_ROWS + 1),
        "c1_options": "0",
    }
    for i, article_id in enumerate(ids):
        data[f"base_{i}_qty"] = "1"
        data[f"base_{i}_article"] = str(article_id)
    data[f"base_{MAX_ROWS}_qty"] = "1"
    data[f"base_{MAX_ROWS}_article"] = ""
    response = ready_client.post(f"/articles/{box}/assembled", data=data, follow_redirects=False)
    assert response.status_code == 303
    with sm_of(ready_client)() as s:
        saved = s.scalars(select(ArticleComponent.part_id).where(ArticleComponent.article_id == box)).all()
    assert sorted(saved) == sorted(ids)


def test_edit_form_at_the_limit_shows_no_row_beyond_it(ready_client):
    box, _ids = _box_with_max_rows(ready_client)
    page = ready_client.get(f"/articles/{box}/assembled").text
    assert f'name="base_rows" value="{MAX_ROWS}"' in page
    assert f'name="base_{MAX_ROWS}_article"' not in page
