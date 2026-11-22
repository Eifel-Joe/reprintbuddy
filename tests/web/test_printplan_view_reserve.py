"""The reserve of the reload note on the overview (Spec 10.2 "Reserve", p720): the setting reaches the plan.

09:00 UTC is 10:00 on the page (Europe/Berlin in November). One article of 200 g, two prints 10:00-11:00 and
11:05-12:05 (200 g an hour), 300 g in the slot: without reserve dry at 11:35; with 30 g at 11:26; with 50 g at
11:20.
"""

from reprintbuddy import settings as st
from reprintbuddy.domain.printerboard import FileFacts
from factories import make_entry
from test_printplan_view_filament import article, board, status, texts, view

FACTS = {(1, "v1"): FileFacts(None, {1: 3600})}


def plan(db, lang="en"):
    with db.begin() as s:
        make_entry(s, article(s, grams=200).id, 2)
    return view(db, board(status(1), grams={1: {(0, 1): 300.0}}, facts=FACTS), lang=lang)


def test_default_reserve_of_30_g_without_any_setting_stored(db):
    v = plan(db)
    assert texts(v.lanes[0]) == ["Reload about 11:26 · PLA #000000 (AMS 1, slot 2)"]
    assert v.reload == "Reload needed: Printer 1 about 11:26"


def test_the_stored_grams_count(db):
    with db.begin() as s:
        st.set_setting(s, "plan_reserve_grams", 50, commit=False)
    assert texts(plan(db, "de").lanes[0]) == ["Nachladen ca. 11:20 · PLA #000000 (AMS 1, Slot 2)"]


def test_switched_off_the_plan_reckons_to_0_g(db):
    with db.begin() as s:
        st.set_setting(s, "plan_reserve", False, commit=False)
        st.set_setting(s, "plan_reserve_grams", 50, commit=False)
    assert texts(plan(db).lanes[0]) == ["Reload about 11:35 · PLA #000000 (AMS 1, slot 2)"]
