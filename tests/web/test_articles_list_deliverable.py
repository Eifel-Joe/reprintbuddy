"""Articles made of parts built on order are deliverable or not, like sets: the article list filters them
and names the ones that are not deliverable in its key figures; the stock tile of the overview leaves them
out (p363, Spec 10.1, 10.2)."""

import re

from conftest import sm_of
from factories import make_article, make_assembled
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def _articles(client):
    with sm_of(client).begin() as s:
        enough = make_article(s, name="Segment", stock=24).id
        none = make_article(s, name="Socket", stock=0).id
        make_assembled(s, name="Hose ready", sku="EX-H1", mode="order", base=[(enough, 12)])
        make_assembled(s, name="Hose waiting", sku="EX-H2", mode="order", base=[(enough, 1), (none, 1)])


def _filters(page: str) -> dict[str, str]:
    nav = re.search(r'<nav class="filters">(.*?)</nav>', page, re.S).group(1)
    return {
        re.sub(r"\s+", " ", label).strip(): count
        for label, count in re.findall(r'<a class="filter[^"]*" href="[^"]*"[^>]*>(.*?)<span class="count">(\d+)</span>', nav, re.S)
    }


def test_filters_know_deliverable_and_not_deliverable(ready_client):
    _articles(ready_client)
    filters = _filters(ready_client.get("/articles").text)
    assert filters[en("status.article.deliverable")] == "1"
    assert filters[en("status.article.not_deliverable")] == "1"
    shown = ready_client.get("/articles?status=not_deliverable").text
    assert "Hose waiting" in shown and "Hose ready" not in shown
    shown = ready_client.get("/articles?status=deliverable").text
    assert "Hose ready" in shown and "Hose waiting" not in shown


def test_key_figures_name_the_not_deliverable_ones(ready_client):
    _articles(ready_client)
    page = ready_client.get("/articles").text
    card = re.search(
        rf'<span class="kpi-value">(\d+)</span><span class="kpi-label">{en("status.article.not_deliverable")}</span>', page
    )
    assert card is not None and card.group(1) == "1"


def test_the_stock_tile_still_leaves_them_out(ready_client):  # Spec 10.2
    _articles(ready_client)
    page = ready_client.get("/").text
    assert "status=not_deliverable" not in page and "status=deliverable" not in page
