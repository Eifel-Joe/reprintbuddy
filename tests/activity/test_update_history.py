"""p643: every update has its start and result in the log, also when the next run starts within a minute.

The state file of the update helper holds one run; the log reads it at the start of the service and once a
minute. A run that ended while the new process still saw "running" (the helper checks the health after the
restart) and was overwritten by the next run within that minute never reached the log (Spec 11.3a, 10.7).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from reprintbuddy.activity import states
from reprintbuddy.activity.writer import ActivityLog
from reprintbuddy.models import ActivityEntry, ActivityMark
from reprintbuddy.ops import status as st
from reprintbuddy.ops.status import UpdateStatus, read_history, write_status

A = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
B = A + timedelta(minutes=4)


def lines(db) -> list[tuple[str, dict]]:
    with db() as s:
        return [(e.kind, e.details) for e in s.scalars(select(ActivityEntry).order_by(ActivityEntry.at, ActivityEntry.id))]


def log_for(db, data_dir) -> ActivityLog:
    return ActivityLog(db, update_result=lambda: states.update_outcomes(data_dir))


def test_a_run_overwritten_by_the_next_within_the_minute_is_still_logged(db, tmp_path):
    activity = log_for(db, tmp_path)
    write_status(tmp_path, UpdateStatus(state="running", step="health", from_tag="v1.2.0-beta.1",
                                        to_tag="v1.2.0-beta.2", started_at=A))
    activity.check_update()  # the new process starts while the helper checks its health
    activity.drain()
    write_status(tmp_path, UpdateStatus(state="succeeded", step="done", from_tag="v1.2.0-beta.1",
                                        to_tag="v1.2.0-beta.2", started_at=A, finished_at=A + timedelta(minutes=2)))
    # Within the minute the way back starts and overwrites the file.
    write_status(tmp_path, UpdateStatus(state="running", step="stop", from_tag="v1.2.0-beta.2", to_tag="v1.1.0",
                                        started_at=B, kind="return", return_case=1))
    write_status(tmp_path, UpdateStatus(state="rolled_back", step="rollback", from_tag="v1.2.0-beta.2",
                                        to_tag="v1.1.0", started_at=B, finished_at=B + timedelta(minutes=1),
                                        kind="return", return_case=1))
    activity.check_update()
    activity.drain()
    assert lines(db) == [
        ("update_started", {"from": "v1.2.0-beta.1", "to": "v1.2.0-beta.2"}),
        ("update_result", {"from": "v1.2.0-beta.1", "to": "v1.2.0-beta.2", "result": "done"}),
        ("update_started", {"from": "v1.2.0-beta.2", "to": "v1.1.0", "return": True}),
        ("update_result", {"from": "v1.2.0-beta.2", "to": "v1.1.0", "result": "rolled_back", "return": True}),
    ]
    for _ in range(3):  # every minute and at every start: nothing twice
        activity.check_update()
        activity.drain()
    assert len(lines(db)) == 4


def test_a_run_older_than_the_mark_is_not_logged_again(db, tmp_path):
    with db.begin() as s:
        s.add(ActivityMark(key="update.result", state=B.isoformat(), at=B.replace(tzinfo=None)))
    write_status(tmp_path, UpdateStatus(state="succeeded", started_at=A, from_tag="a", to_tag="b"))
    write_status(tmp_path, UpdateStatus(state="succeeded", started_at=B, from_tag="b", to_tag="c"))
    activity = log_for(db, tmp_path)
    activity.check_update()
    activity.drain()
    assert lines(db) == []


def test_an_older_helper_without_history_is_read_as_before(db, tmp_path):
    """An update from 1.2.0 runs the helper of 1.2.0, which writes no history: the state file still counts."""
    st.status_path(tmp_path).write_text(json.dumps(UpdateStatus(
        state="failed", started_at=A, from_tag="v1.2.0", to_tag="v1.2.1").to_json()), encoding="utf-8")
    activity = log_for(db, tmp_path)
    activity.check_update()
    activity.drain()
    assert [kind for kind, _ in lines(db)] == ["update_started", "update_result"]


def test_the_history_keeps_final_states_only_and_one_per_run(tmp_path):
    write_status(tmp_path, UpdateStatus(state="queued", started_at=A))
    write_status(tmp_path, UpdateStatus(state="running", started_at=A))
    assert read_history(tmp_path) == []
    write_status(tmp_path, UpdateStatus(state="succeeded", started_at=A))
    write_status(tmp_path, UpdateStatus(state="succeeded", started_at=A, acknowledged=True))  # the card was seen
    history = read_history(tmp_path)
    assert [(h.state, h.started_at, h.acknowledged) for h in history] == [("succeeded", A, True)]


def test_the_history_keeps_the_last_runs(tmp_path):
    for n in range(st.HISTORY_KEEP + 3):
        write_status(tmp_path, UpdateStatus(state="failed", started_at=A + timedelta(hours=n)))
    history = read_history(tmp_path)
    assert len(history) == st.HISTORY_KEEP
    assert history[0].started_at == A + timedelta(hours=3)
    assert history[-1].started_at == A + timedelta(hours=st.HISTORY_KEEP + 2)


def test_a_broken_history_counts_as_empty_and_is_replaced(tmp_path):
    st.history_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert read_history(tmp_path) == []
    write_status(tmp_path, UpdateStatus(state="succeeded", started_at=A))
    assert [h.started_at for h in read_history(tmp_path)] == [A]


def test_an_interrupted_run_goes_into_the_history(tmp_path):
    write_status(tmp_path, UpdateStatus(state="running", step="install", started_at=A))
    st.mark_interrupted(tmp_path, A + timedelta(minutes=30))
    assert [h.state for h in read_history(tmp_path)] == ["rollback_failed"]
