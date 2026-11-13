"""The inventory on the bookings page offers "awaiting assembly" only for print articles (p362, Spec 6.1)."""

import re
from pathlib import Path

import pytest

from node_support import run_harness

from conftest import sm_of
from factories import make_article, make_assembled
from reprintbuddy import __version__

HERE = Path(__file__).parent
SCRIPT = HERE.parents[1] / "src" / "reprintbuddy" / "web" / "static" / "js" / "inventory_field.js"


def test_inventory_marks_articles_made_of_parts(ready_client):
    with sm_of(ready_client).begin() as s:
        panel = make_article(s, name="Panel", stock=3).id
        box = make_assembled(s, name="Storage box", base=[(panel, 2)]).id
    page = ready_client.get("/bookings").text
    form = re.search(r'<form method="post" action="/bookings/inventory">(.*?)</form>', page, re.S).group(1)
    assert re.search(rf'<option value="{box}" data-stock-only>', form)
    assert re.search(rf'<option value="{panel}">', form)
    assert f'<script src="/static/js/inventory_field.js?v={__version__}" defer></script>' in page


@pytest.fixture(scope="module")
def result():
    return run_harness("inventory_field_harness.js", SCRIPT)


def test_awaiting_assembly_is_offered_for_print_articles(result):
    both = {"hidden": False, "disabled": False}
    assert result["none"] == {"value": "awaiting_assembly", "awaiting": both}
    assert result["print"] == {"value": "awaiting_assembly", "awaiting": both}
    assert result["back"] == {"value": "stock", "awaiting": both}


def test_an_article_made_of_parts_counts_only_its_stock(result):
    assert result["assembled"] == {"value": "stock", "awaiting": {"hidden": True, "disabled": True}}
