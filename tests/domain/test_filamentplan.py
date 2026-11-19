"""Whether the filament in a printer lasts for the prints of its lane, and when to reload (Spec 10.2
"Filament in den Druckern", p648). A pure function: no clock, no database."""

from datetime import datetime, timedelta

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
    jobs = [job(10, 12, pla(300))]
    grams = {(0, 0): 200.0, (0, 1): 200.0}
    assert lane_filament(jobs, [BLACK, BLACK_2], grams, True, NOW).markers == ()
    off = lane_filament(jobs, [BLACK, BLACK_2], grams, False, NOW)
    assert kinds(off) == [("reload", at(11, 20), BLACK)]
    assert kinds(lane_filament(jobs, [BLACK, BLACK_2], grams, None, NOW)) == [("reload", at(11, 20), BLACK)]


def test_pool_with_a_slot_of_unknown_grams_is_unknown():
    r = lane_filament([job(10, 12, pla(300))], [BLACK, BLACK_2], {(0, 0): 200.0}, True, NOW)
    assert kinds(r) == [("unknown_stock", None, BLACK)]


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
