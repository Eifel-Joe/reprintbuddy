"""Print-only paths, labels and reorder demand with articles made of parts (0.4.0 p341)."""

import pytest

from factories import make_article, make_assembled
from reprintbuddy.domain import workqueue
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.web import labels_view


# ---- Aufgabe 9: printing and labels --------------------------------------------------------------


def test_manual_entry_only_for_print_articles(db):  # Spec 7.4
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)])
        with pytest.raises(ValidationError) as err:
            workqueue.add_entry(s, box.id, 1, "manual", "user")
        assert err.value.key == "queue.print_only"
        workqueue.add_entry(s, panel.id, 1, "manual", "user")


def test_label_of_assembled_leaves_print_fields_empty(db):  # Teil C 9
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        side = make_article(s, name="Side part L")
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 2)], min_stock=3, buffer=1)
        data = labels_view.article_data(box, "en")
        assert (data.units_per_print, data.print_target) == (None, None)
        assert (data.min_stock, data.buffer, data.target_qty) == (3, 1, 4)
        hose = make_assembled(s, name="Hose", sku="EX-H1", base=[(panel.id, 12)],
                              fields=[[("-L", [(side.id, 1)]), ("-R", [(make_article(s, name="R").id, 1)])]])
        data = labels_view.article_data(hose, "en", has_choices=True)
        assert (data.min_stock, data.buffer, data.target_qty) == (None, None, None)
        printed = labels_view.article_data(panel, "en")
        assert printed.units_per_print == 1
