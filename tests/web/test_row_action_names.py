"""Symbol buttons in table rows name their object for screen readers (v0.3.8 p68).

Several rows show the same "Archive" symbol; read aloud it must say which row it belongs to, as
"Archive: Template A". The visible tooltip (title) stays the short action.
"""

import re
from datetime import datetime
from html import unescape

from conftest import csrf_of, form_token_of, sm_of
from factories import assign_sort, make_article, make_filament_settings, make_order, make_part, make_set, make_sort
from reprintbuddy.models import ArticlePart


def actions_of(html):
    """The (title, aria-label) of every symbol button inside the action cells of a page."""
    cells = re.findall(r'<td class="actions-col">(.*?)</td>', html, re.S)
    found = []
    for cell in cells:
        for tag in re.findall(r"<(?:a|button)\b[^>]*class=\"icon-btn\"[^>]*>", cell):
            title = re.search(r'title="([^"]*)"', tag)
            label = re.search(r'aria-label="([^"]*)"', tag)
            found.append((unescape(title.group(1)) if title else None, unescape(label.group(1)) if label else None))
    return found


def test_article_rows(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template A")
    labels = [label for _, label in actions_of(ready_client.get("/articles").text)]
    assert labels == ["Edit settings: Template A", "Journal: Template A", "Archive: Template A"]
    titles = [title for title, _ in actions_of(ready_client.get("/articles").text)]
    assert titles == ["Edit settings", "Journal", "Archive"]  # the tooltip stays short


def test_archived_article_rows(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template A", archived_at=datetime(2026, 1, 1))
    labels = [label for _, label in actions_of(ready_client.get("/articles?view=archived").text)]
    assert labels == ["Journal: Template A", "Restore: Template A"]


def test_part_rows(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Insert M4")
    labels = [label for _, label in actions_of(ready_client.get("/parts").text)]
    assert labels == ["Edit part: Insert M4", "Journal: Insert M4", "Archive: Insert M4"]


def test_set_rows(ready_client):
    with sm_of(ready_client).begin() as s:
        art = make_article(s, name="Template A")
        make_set(s, "Starter Set", [(1, art.id)])
    labels = [label for _, label in actions_of(ready_client.get("/sets").text)]
    assert labels == ["Edit set: Starter Set", "Archive: Starter Set"]


def test_order_rows(ready_client):
    with sm_of(ready_client).begin() as s:
        make_order(s, reference="Order 1001")
    labels = [label for _, label in actions_of(ready_client.get("/orders").text)]
    assert labels == ["View: Order 1001", "Cancel order: Order 1001"]


def test_filament_rows(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort, name="Uses sort")
    labels = [label for _, label in actions_of(bb_client.get("/filament").text)]
    assert labels == ["Configure: Example Brand PLA Black"]


def test_reorder_received_button(bb_client):
    with sm_of(bb_client).begin() as s:
        part = make_part(s, name="Insert M4", stock=10, pack_size=50, min_order_packs=1, lead_time_days=0,
                         safety_value=20, safety_unit="qty")
        art = make_article(s, needs_assembly=True, stock=0, min_stock=3, buffer=0)
        s.add(ArticlePart(article_id=art.id, part_id=part.id, qty_per_unit=2))
        s.flush()
        pid = part.id
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    bb_client.post("/reorder/mark", data={
        "csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
        "item_id": str(pid), "packs": "1",
    })
    labels = [label for _, label in actions_of(bb_client.get("/reorder").text)]
    # v0.3.16 p280: an order on its way can also be taken back; that button names its row too.
    assert labels == ["Arrived: Insert M4", "Take back: Insert M4"], labels


def test_queue_rows_name_their_article(bb_client):
    from factories import make_entry

    with sm_of(bb_client).begin() as s:
        first = make_article(s, name="Template A")
        second = make_article(s, name="Template B")
        make_entry(s, first.id)
        make_entry(s, second.id)
    html = bb_client.get("/queue").text
    assert 'aria-label="Drag to move: Template A"' in html and 'aria-label="Drag to move: Template B"' in html
    assert 'aria-label="More actions: Template A"' in html and 'aria-label="More actions: Template B"' in html


def test_a_name_with_quotes_and_markup_stays_inside_the_attribute(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name='A "x" <b>y</b> & z')
    html = ready_client.get("/articles").text
    assert "<b>y</b>" not in html
    assert 'aria-label="Archive: A &#34;x&#34; &lt;b&gt;y&lt;/b&gt; &amp; z"' in html

