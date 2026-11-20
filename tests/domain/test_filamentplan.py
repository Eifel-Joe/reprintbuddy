"""Whether the filament in a printer lasts for the prints of its lane, and when to reload (Spec 10.2
"Filament in den Druckern", p648). A pure function: no clock, no database."""

from datetime import datetime, timedelta

import pytest

from reprintbuddy.bambuddy.types import AmsSlot
from reprintbuddy.domain.filamentplan import LanePrint, Marker, Need, lane_filament

NOW = datetime(2026, 11, 19, 10, 0)


def at(hour: int, minute: int = 0) -> datetime:
    return NOW.replace(hour=hour, minute=minute)


BLACK = AmsSlot(0, 0, "PLA", "#000000", True)
BLACK_2 = AmsSlot(0, 1, "PLA", "#000000", True)
RED = AmsSlot(0, 2, "PETG", "#FF0000", True)
DARK_RED = AmsSlot(0, 3, "PETG", "#E01010", True)


def job(start: int, end: int, *needs: Need, print_start: datetime | None = None) -> LanePrint:
    s, e = at(start), at(end)
    return LanePrint(start=s, end=e, print_start=print_start or s, needs=tuple(needs))


def unknown(start: int, end: int) -> LanePrint:
    return LanePrint(start=at(start), end=at(end), print_start=at(start), needs=None)


def pla(grams: float) -> Need:
    return Need("PLA", "#000000", grams)


def kinds(result):
    return [(m.kind, m.at, m.slot) for m in result.markers]


def test_enough_gives_no_marker():
    r = lane_filament([job(10, 11, pla(100)), job(11, 12, pla(100))], [BLACK], {(0, 0): 300.0}, None, NOW)
    assert r.markers == () and r.more == 0 and r.unknown_needs == ()


def test_reload_inside_the_print_that_runs_out_linear():
    """300 g left; the first print takes 200, the second 200 over 11:00-13:00: empty half way, at 12:00."""
    r = lane_filament([job(10, 11, pla(200)), job(11, 13, pla(200))], [BLACK], {(0, 0): 300.0}, None, NOW)
    assert r.markers == (Marker("reload", "PLA", "#000000", BLACK, at(12), due=False, latest=False),)


def test_running_print_counts_whole_and_a_past_moment_is_now():
    """Bambuddy books the use only at the end: the running print counts whole. It started 09:00 (2 h,
    ends 11:00) and needs 100 g of 40 g left: empty at 09:48, already past → reload now."""
    running = job(10, 11, pla(100), print_start=at(9))
    r = lane_filament([running], [BLACK], {(0, 0): 40.0}, None, NOW)
    (m,) = r.markers
    assert m.kind == "reload" and m.due is True


def test_only_the_first_place_per_sort():
    r = lane_filament([job(10, 11, pla(200)), job(11, 12, pla(200)), job(12, 13, pla(200))], [BLACK],
                      {(0, 0): 100.0}, None, NOW)
    assert len(r.markers) == 1 and r.markers[0].at == at(10, 30)


def test_backup_on_pools_same_material_and_colour():
    """Together the two slots last; the first runs empty on the way (p716: a calm note, no reload)."""
    jobs = [job(10, 12, pla(300))]
    grams = {(0, 0): 200.0, (0, 1): 200.0}
    on = lane_filament(jobs, [BLACK, BLACK_2], grams, True, NOW)
    assert slots(on) == [("slot_empty", at(11, 20), BLACK, BLACK_2)]
    off = lane_filament(jobs, [BLACK, BLACK_2], grams, False, NOW)
    assert kinds(off) == [("reload", at(11, 20), BLACK)]
    assert kinds(lane_filament(jobs, [BLACK, BLACK_2], grams, None, NOW)) == [("reload", at(11, 20), BLACK)]


@pytest.mark.parametrize("prefer_lowest", [True, False, None])
def test_pool_with_a_slot_of_unknown_grams_is_unknown(prefer_lowest):
    """p716: a slot without grams makes the whole sort unknown, as before; no slot notes."""
    r = lane_filament([job(10, 12, pla(300))], [BLACK, BLACK_2], {(0, 0): 200.0}, True, NOW,
                      prefer_lowest=prefer_lowest)
    assert kinds(r) == [("unknown_stock", None, BLACK)]


# ---- p716: the slots of one sort run empty one after the other (Spec 10.2 "Vorrat", "Nachladen") ----------------


def slots(result):
    return [(m.kind, m.at, m.slot, m.next_slot) for m in result.markers]


def test_lowest_spool_first_slot_note_then_reload():
    """Lowest spool first on: slot 2 (100 g) before slot 1 (300 g). 200 g over 10-12 empties slot 2 at 11:00;
    400 g over 12-14 empties the sort half way, at 13:00, in slot 1, the slot then in use."""
    jobs = [job(10, 12, pla(200)), job(12, 14, pla(400))]
    grams = {(0, 0): 300.0, (0, 1): 100.0}
    r = lane_filament(jobs, [BLACK, BLACK_2], grams, True, NOW, prefer_lowest=True)
    assert slots(r) == [("slot_empty", at(11), BLACK_2, BLACK), ("reload", at(13), BLACK, None)]
    assert [m.due for m in r.markers] == [False, False] and r.more == 0


@pytest.mark.parametrize("prefer_lowest", [False, None])
def test_lowest_spool_first_off_or_unknown_takes_the_slot_order(prefer_lowest):
    """Off or unknown: slot 1 (300 g) first, empty at 12:30 in the second print; the sort at 13:00 in slot 2."""
    jobs = [job(10, 12, pla(200)), job(12, 14, pla(400))]
    grams = {(0, 0): 300.0, (0, 1): 100.0}
    r = lane_filament(jobs, [BLACK, BLACK_2], grams, True, NOW, prefer_lowest=prefer_lowest)
    assert slots(r) == [("slot_empty", at(12, 30), BLACK, BLACK_2), ("reload", at(13), BLACK_2, None)]


def test_lowest_spool_first_then_the_next_lowest_and_ties_in_slot_order():
    """Three slots of 300, 100 and 100 g: the two of 100 g first, in slot order, then the 300 g."""
    third = AmsSlot(1, 0, "PLA", "#000000", True)
    grams = {(0, 0): 300.0, (0, 1): 100.0, (1, 0): 100.0}
    r = lane_filament([job(10, 15, pla(500))], [BLACK, BLACK_2, third], grams, True, NOW, prefer_lowest=True)
    assert slots(r) == [("slot_empty", at(11), BLACK_2, third), ("slot_empty", at(12), third, BLACK)]


@pytest.mark.parametrize("backup", [False, None])
def test_backup_off_or_unknown_stays_as_before(backup):
    """Without the backup the setting changes nothing: the chosen slot alone, no slot notes."""
    jobs = [job(10, 12, pla(200)), job(12, 14, pla(400))]
    grams = {(0, 0): 300.0, (0, 1): 100.0}
    r = lane_filament(jobs, [BLACK, BLACK_2], grams, backup, NOW, prefer_lowest=True)
    assert slots(r) == [("reload", at(12, 30), BLACK, None)]


def test_one_slot_of_the_sort_stays_as_before():
    r = lane_filament([job(10, 12, pla(400))], [BLACK, RED], {(0, 0): 100.0, (0, 2): 900.0}, True, NOW,
                      prefer_lowest=True)
    assert slots(r) == [("reload", at(10, 30), BLACK, None)]


def test_a_slot_used_up_exactly_is_noted_where_the_next_print_starts():
    """100 g in slot 1 and a print of 100 g: it lasts; the next print starts on slot 2, so slot 1 is empty then."""
    jobs = [job(10, 11, pla(100)), job(11, 12, pla(50))]
    r = lane_filament(jobs, [BLACK, BLACK_2], {(0, 0): 100.0, (0, 1): 100.0}, True, NOW)
    assert slots(r) == [("slot_empty", at(11), BLACK, BLACK_2)]


def test_running_print_counts_whole_for_the_slots_and_a_past_slot_note_is_now():
    """Running since 09:00 until 11:00, 100 g whole: slot 1 (30 g) was empty at 09:36, already past."""
    running = job(10, 11, pla(100), print_start=at(9))
    r = lane_filament([running], [BLACK, BLACK_2], {(0, 0): 30.0, (0, 1): 200.0}, True, NOW)
    (m,) = r.markers
    assert (m.kind, m.at, m.due, m.slot, m.next_slot) == ("slot_empty", at(9, 36), True, BLACK, BLACK_2)


def test_slot_note_after_an_unknown_need_is_at_the_latest():
    r = lane_filament([unknown(10, 11), job(11, 12, pla(100))], [BLACK, BLACK_2],
                      {(0, 0): 50.0, (0, 1): 500.0}, True, NOW)
    (m,) = r.markers
    assert m.kind == "slot_empty" and m.latest is True and m.at == at(11, 30)


def test_at_most_three_notes_the_reload_before_slot_notes():
    """Four slots of one sort all run empty: three slot notes and the reload, four in all. The orange note
    stays; the calm ones fill up the three, shown in time order (Spec 10.2: slot notes count to the three)."""
    four = [AmsSlot(0, i, "PLA", "#000000", True) for i in range(4)]
    grams = {(0, i): 100.0 for i in range(4)}
    r = lane_filament([job(10, 15, pla(500))], four, grams, True, NOW)
    assert slots(r) == [("slot_empty", at(11), four[0], four[1]), ("slot_empty", at(12), four[1], four[2]),
                        ("reload", at(14), four[3], None)]
    assert r.more == 1 and r.hidden_reloads == ()


def test_slot_notes_come_after_not_loaded_and_unknown_stock():
    """A reload, "not loaded" and "stock unknown" go first; the calm slot note is counted in "+ n"."""
    white = AmsSlot(1, 0, "PLA", "#FFFFFF", True)
    jobs = [job(10, 12, pla(300), Need("PETG", "#FF0000", 10), Need("PLA", "#FFFFFF", 10)),
            job(12, 14, pla(300))]
    grams = {(0, 0): 200.0, (0, 1): 200.0}
    r = lane_filament(jobs, [BLACK, BLACK_2, white], grams, True, NOW)
    assert [m.kind for m in r.markers] == ["reload", "not_loaded", "unknown_stock"] and r.more == 1


def test_slot_notes_do_not_count_as_reloads():
    """The head line names only "Reload needed" (Spec 10.2): slot notes are never hidden reloads."""
    four = [AmsSlot(0, i, "PLA", "#000000", True) for i in range(4)]
    r = lane_filament([job(10, 15, pla(500))], four, {(0, i): 200.0 for i in range(4)}, True, NOW)
    assert [m.kind for m in r.markers] == ["slot_empty", "slot_empty"] and r.hidden_reloads == ()


def test_slot_choice_exact_colour_then_similar_then_material():
    """Exact colour first; else a colour within 40 per channel; else the material alone; each slot once."""
    red_need = Need("PETG", "#FF0000", 50)
    near = Need("PETG", "#F00505", 50)
    any_petg = Need("PETG", "#0000FF", 50)
    grams = {(0, 2): 10.0, (0, 3): 1000.0}
    r = lane_filament([job(10, 11, red_need)], [DARK_RED, RED], grams, None, NOW)
    assert kinds(r)[0][2] == RED  # exact, though later in slot order
    r = lane_filament([job(10, 11, near)], [RED, DARK_RED], {(0, 2): 10.0, (0, 3): 1000.0}, None, NOW)
    assert kinds(r)[0][2] == RED  # similar, first in slot order
    r = lane_filament([job(10, 11, any_petg)], [RED, DARK_RED], {(0, 2): 10.0, (0, 3): 1000.0}, None, NOW)
    assert kinds(r)[0][2] == RED  # material only, first in slot order


def test_each_slot_once_per_print():
    """Two needs of the same sort in one print take two slots (Bambuddy's `used_tray_ids`)."""
    r = lane_filament([job(10, 11, pla(50), pla(50))], [BLACK, BLACK_2], {(0, 0): 60.0, (0, 1): 10.0}, None, NOW)
    assert kinds(r) == [("reload", at(10, 12), BLACK_2)]


def test_material_case_and_alpha_free_colour():
    r = lane_filament([job(10, 11, Need(" pla ", "#000000", 50))], [BLACK], {(0, 0): 10.0}, None, NOW)
    assert kinds(r)[0][2] == BLACK


def test_not_loaded_and_unknown_stock_have_no_time():
    jobs = [job(10, 11, Need("ABS", "#FFFFFF", 20), pla(10)), job(11, 12, Need("ABS", "#FFFFFF", 20))]
    r = lane_filament(jobs, [BLACK], {}, None, NOW)
    assert r.markers == (
        Marker("not_loaded", "ABS", "#FFFFFF", None, None, due=False, latest=False),
        Marker("unknown_stock", "PLA", "#000000", BLACK, None, due=False, latest=False),
    )


def test_empty_or_unloaded_slot_does_not_count():
    empty = AmsSlot(0, 0, "PLA", "#000000", False)
    r = lane_filament([job(10, 11, pla(10))], [empty], {(0, 0): 500.0}, None, NOW)
    assert [m.kind for m in r.markers] == ["not_loaded"]


def test_unknown_need_is_noted_and_later_reloads_are_at_the_latest():
    r = lane_filament([unknown(10, 11), job(11, 12, pla(100))], [BLACK], {(0, 0): 50.0}, None, NOW)
    assert r.unknown_needs == (0,)
    assert r.markers[0].latest is True and r.markers[0].at == at(11, 30)


def test_at_most_three_markers_reloads_first_then_the_rest():
    slots = [AmsSlot(0, i, m, c, True) for i, (m, c) in enumerate(
        [("PLA", "#000000"), ("PLA", "#FFFFFF"), ("PETG", "#000000"), ("ABS", "#000000")])]
    needs = [Need("PLA", "#000000", 100), Need("PLA", "#FFFFFF", 100), Need("PETG", "#000000", 100),
             Need("ABS", "#000000", 100), Need("TPU", "#000000", 5)]
    grams = {(0, 0): 90.0, (0, 1): 10.0, (0, 2): 50.0}  # ABS unknown, TPU not loaded
    r = lane_filament([job(10, 12, *needs)], slots, grams, None, NOW)
    assert [m.kind for m in r.markers] == ["reload", "reload", "reload"] and r.more == 2
    assert [m.at for m in r.markers] == sorted(m.at for m in r.markers)


def test_zero_or_missing_grams_need_nothing():
    r = lane_filament([job(10, 11, pla(0))], [], {}, None, NOW)
    assert r.markers == ()


def test_need_without_material_takes_the_colour():
    r = lane_filament([job(10, 11, Need(None, "#FF0000", 30))], [BLACK, RED], {(0, 2): 10.0}, None, NOW)
    assert kinds(r)[0][2] == RED


def test_length_is_from_print_start_to_end():
    long = LanePrint(start=at(10), end=at(14), print_start=at(10), needs=(pla(400),))
    r = lane_filament([long], [BLACK], {(0, 0): 100.0}, None, NOW)
    assert r.markers[0].at == at(10) + timedelta(hours=1)


def test_each_sort_only_once_even_over_two_slots():
    """Review p648, 7: backup off, two slots of black PLA both run dry: one note for the sort (Spec 10.2)."""
    r = lane_filament([job(10, 11, pla(50), pla(50))], [BLACK, BLACK_2], {(0, 0): 10.0, (0, 1): 10.0}, False, NOW)
    assert len(r.markers) == 1
