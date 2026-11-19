"""Grams in the printers and the filament needs of foreign prints, for the reload hint of the print plan
(Spec 7.6, 10.2, p648): read with the regular query for printers that have work, and again a minute after a
print ended there (Bambuddy books the use only after it reports the end, `main.py:9501` vs `:9930`)."""

import asyncio

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import AmsSlot, FilamentRequirement
from reprintbuddy.domain import engine as engine_module

BLACK = AmsSlot(0, 0, "PLA", "#000000", True)


def _stop_queue(env) -> None:
    with env.sm.begin() as s:
        st.set_setting(s, "queue_running", False)


def _no_rule(env) -> None:
    with env.sm.begin() as s:
        st.set_setting(s, "automation", False)


async def test_grams_are_read_for_printers_with_work_only(engine_env):
    """Reprintbuddy's queue holds an A1 print: printers 1 and 2 (A1) have work, printer 3 (P1S) has none."""
    env = engine_env
    _stop_queue(env)
    env.fake.set_printer_slots(1, [BLACK], grams={(0, 0): 640.0})

    await env.engine.tick(poll=True)

    assert env.fake.calls.count("inventory_remain") == 2
    assert env.engine.board.slot_grams == {1: {(0, 0): 640.0}, 2: {}}


async def test_no_work_no_request(engine_env):
    env = engine_env
    _no_rule(env)
    await env.engine.tick(poll=True)
    assert "inventory_remain" not in env.fake.calls and env.engine.board.slot_grams == {}


async def test_printing_printer_and_pinned_foreign_entry_count_as_work(engine_env):
    env = engine_env
    _no_rule(env)
    env.fake.set_printer_status(3, state="RUNNING", progress=10.0, remaining_min=30)
    env.fake.add_foreign_item("Lid.gcode.3mf", printer_id=2)
    await env.engine.tick(poll=True)
    assert set(env.engine.board.slot_grams) == {2, 3}


async def test_printer_out_of_service_is_not_asked(engine_env):
    env = engine_env
    _stop_queue(env)
    env.fake.set_printer_status(2, connected=False)
    await env.engine.tick(poll=True)
    assert set(env.engine.board.slot_grams) == {1}


async def test_a_refusal_leaves_that_printer_unknown_and_goes_on(engine_env):
    env = engine_env
    _stop_queue(env)
    env.fake.fail_next("inventory_remain", BambuddyRejected("no", status=403))
    await env.engine.tick(poll=True)
    assert set(env.engine.board.slot_grams) == {2}
    assert env.engine.status.poll_error is None and env.engine.status.reachable is True


async def test_unreachable_stops_at_the_first_printer(engine_env):
    env = engine_env
    _stop_queue(env)
    env.fake.fail_next("inventory_remain", BambuddyUnavailable("gone"))
    await env.engine.tick(poll=True)
    assert env.fake.calls.count("inventory_remain") == 1


async def test_needs_of_foreign_prints_from_the_file_manager(engine_env):
    """Read once per file and plate; an own print or one without a library file asks nothing."""
    env = engine_env
    _no_rule(env)
    need = [FilamentRequirement(1, 12.5, "PLA", "#000000")]
    env.fake.set_requirements(2, 1, need)
    env.fake.add_foreign_item("B.gcode.3mf", library_file_id=2, plate_id=1, target_model="A1")
    env.fake.add_foreign_item("Archive print", target_model="A1")  # no library file

    await env.engine.tick(poll=True)
    await env.engine.tick(poll=True)

    assert env.engine.board.needs == {(2, 1): tuple(need)}
    assert env.fake.calls.count("filament_requirements") == 1


async def test_a_short_tick_reads_nothing(engine_env):
    env = engine_env
    _stop_queue(env)
    await env.engine.tick(poll=False)
    assert "inventory_remain" not in env.fake.calls


async def test_print_end_reads_the_grams_again_after_a_delay(engine_env, monkeypatch):
    """A print ended on printer 1: a minute later the grams are read again for that printer only."""
    env = engine_env
    _stop_queue(env)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        delays.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(engine_module.asyncio, "sleep", fake_sleep)
    env.fake.set_printer_status(1, state="RUNNING", progress=90.0, remaining_min=5)
    await env.engine.tick(poll=True)
    before = env.fake.calls.count("inventory_remain")
    env.fake.set_printer_slots(1, [BLACK], grams={(0, 0): 100.0})

    env.engine.board.update(env.fake._statuses[1].__class__(1, True, "FINISH", 100.0, 0, None, False, False))
    await env.engine.wait_for_rereads()

    assert [d for d in delays if d] == [engine_module.REREAD_AFTER_END_SECONDS]  # sleep(0) of other code aside
    assert env.fake.calls.count("inventory_remain") == before + 1
    assert env.engine.board.slot_grams[1] == {(0, 0): 100.0}


async def test_print_end_rereads_at_most_once_a_minute(engine_env, monkeypatch):
    env = engine_env
    _stop_queue(env)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)
    clock = {"t": 1000.0}
    monkeypatch.setattr(env.engine, "_monotonic", lambda: clock["t"])
    await env.engine.tick(poll=True)
    before = env.fake.calls.count("inventory_remain")

    env.engine.print_ended(1)
    await env.engine.wait_for_rereads()
    clock["t"] += 30
    env.engine.print_ended(1)  # within the minute: dropped
    await env.engine.wait_for_rereads()
    clock["t"] += 31
    env.engine.print_ended(1)
    await env.engine.wait_for_rereads()

    assert env.fake.calls.count("inventory_remain") == before + 2


async def test_board_reports_a_print_end_from_a_query_or_a_live_message():
    from reprintbuddy.bambuddy.types import Printer, PrinterStatus
    from reprintbuddy.domain.printerboard import PrinterBoard

    ended: list[int] = []
    board = PrinterBoard(None, on_print_end=ended.append)
    printers = [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "A1")]
    running = PrinterStatus(1, True, "RUNNING", 50.0, 10, None, False, False)
    idle = PrinterStatus(1, True, "IDLE", None, None, None, False, False)
    board.replace(printers, {1: running, 2: idle})
    assert ended == []  # nothing known before: no end
    board.update(idle)
    assert ended == [1]
    board.replace(printers, {1: running})
    board.replace(printers, {1: PrinterStatus(1, False, None, None, None, None, False, False)})
    assert ended == [1]  # gone offline: not an end


async def test_an_empty_requirements_list_is_unknown_not_nothing(engine_env):
    """Review p648, 1: Bambuddy answers `filaments: []` for a file it cannot read the needs of."""
    env = engine_env
    _no_rule(env)
    env.fake.add_foreign_item("B.gcode.3mf", library_file_id=2, plate_id=1, target_model="A1")
    await env.engine.tick(poll=True)
    assert env.engine.board.needs == {(2, 1): None}


async def test_an_odd_answer_is_a_refusal_not_gone(engine_env):
    """Review p648, 5: `BambuddyUnexpected` is a kind of unavailable; one odd answer must not end the reading."""
    from reprintbuddy.bambuddy.client import BambuddyUnexpected

    env = engine_env
    _stop_queue(env)
    env.fake.fail_next("inventory_remain", BambuddyUnexpected("odd"))
    await env.engine.tick(poll=True)
    assert env.fake.calls.count("inventory_remain") == 2 and set(env.engine.board.slot_grams) == {2}


async def test_reread_waits_for_the_tick_and_skips_during_an_update(engine_env, monkeypatch):
    """Review p648, 4: the re-read takes the engine's lock and does nothing while an update runs."""
    env = engine_env

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)
    await env.engine.tick(poll=True)
    before = env.fake.calls.count("inventory_remain")
    env.engine.update_running = lambda: True
    env.engine.print_ended(1)
    await env.engine.wait_for_rereads()
    assert env.fake.calls.count("inventory_remain") == before


async def test_reread_failure_of_any_kind_is_logged_not_raised(engine_env, monkeypatch, caplog):
    env = engine_env

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(engine_module.asyncio, "sleep", no_sleep)
    env.fake.fail_next("inventory_remain", RuntimeError("boom"))
    env.engine.print_ended(1)
    await env.engine.wait_for_rereads()  # no exception escapes
    assert "re-read" in caplog.text


async def test_rereads_are_cancelled_on_stop(engine_env):
    env = engine_env
    env.engine.print_ended(1)
    (task,) = env.engine.reread_tasks()
    await env.engine.cancel_rereads()
    assert task.cancelled() and env.engine.reread_tasks() == []


async def test_a_regular_read_keeps_grams_of_printers_it_did_not_ask(engine_env):
    """Review p648, 4 (race): the reading merges per printer instead of replacing what a re-read stored."""
    env = engine_env
    _stop_queue(env)
    env.engine.board.slot_grams[3] = {(0, 0): 5.0}  # printer 3 has no work: dropped, printers 1, 2 set
    await env.engine.tick(poll=True)
    assert set(env.engine.board.slot_grams) == {1, 2}
