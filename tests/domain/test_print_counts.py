"""The prints of the overview card "Prints" (Spec 10.2): running and waiting.

Running is what Bambuddy runs: its queue entry is `printing` (decided 11.11.2026: Bambuddy sets it
right before the start command, so heating and levelling count as printing). Waiting is Reprintbuddy's
queue (held entries too) plus what is handed over and not started: `sending`, `unclear`, `queued`.
Only these states are named; any other state (finished, failed, a state added later) counts nowhere.
"""

from factories import make_article, make_dispatch, make_entry
from reprintbuddy.domain import workqueue as wq


def test_nothing(db):
    with db.begin() as s:
        assert wq.print_counts(s) == (0, 0)


def test_running_and_waiting(db):
    with db.begin() as s:
        a = make_article(s).id
        b = make_article(s, name="Other").id
        make_entry(s, a, prints=2)
        make_entry(s, b, prints=1, held=True)  # held: still in the queue
        make_dispatch(s, a, status="printing")
        make_dispatch(s, b, status="printing")
        for state in ("sending", "unclear", "queued"):
            make_dispatch(s, a, status=state)
        for state in ("completed", "failed", "cancelled", "missing", "returned"):
            make_dispatch(s, b, status=state)
    with db.begin() as s:
        assert wq.print_counts(s) == (2, 3 + 3)
