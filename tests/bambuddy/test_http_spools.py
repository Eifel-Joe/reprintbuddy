"""Reading the inventory mode and the spools through `HttpBambuddyClient` (B2 Task 4b)."""

from datetime import datetime

import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable, BambuddyUnexpected
from reprintbuddy.bambuddy.parse import parse_spool_mode, parse_spools
from reprintbuddy.bambuddy.types import Spool

OWN = {"enabled": False, "connected": False, "url": None, "native_tags": False}
SPOOLMAN_PATH = f"{API}/spoolman/inventory/spools"
BUILTIN_PATH = f"{API}/inventory/spools"


def _trap(respx_mock, url):
    return respx_mock.get(url).mock(side_effect=AssertionError(f"{url} must not be read"))


@pytest.mark.parametrize(
    "status, mode",
    [
        (load("spoolman_status.json"), "spoolman"),
        (OWN, "builtin"),
        ({"enabled": True, "connected": False, "url": None, "native_tags": False}, "builtin"),
        ({"enabled": True, "connected": False, "url": "", "native_tags": False}, "builtin"),
        ({"enabled": True, "connected": False, "url": "http://spoolman.test:7912", "native_tags": False}, "spoolman"),
        ({"enabled": False, "connected": False, "url": "http://spoolman.test:7912"}, "builtin"),
    ],
)
async def test_spool_mode(respx_mock, status, mode):
    route = respx_mock.get(f"{API}/spoolman/status").respond(200, json=status)
    assert await client().spool_mode() == mode
    assert route.called


@pytest.mark.parametrize("body", [{"enabled": "yes", "url": "x"}, [], {"url": "x"}, {"enabled": True, "url": 5}])
async def test_spool_mode_unexpected(respx_mock, body):
    respx_mock.get(f"{API}/spoolman/status").respond(200, json=body)
    with pytest.raises(BambuddyUnexpected):
        await client().spool_mode()


async def test_spool_mode_forbidden(respx_mock):
    respx_mock.get(f"{API}/spoolman/status").respond(403, json={"detail": "Missing permission"})
    with pytest.raises(BambuddyRejected) as info:
        await client().spool_mode()
    assert info.value.status == 403


def test_parse_spool_mode_directly():
    assert parse_spool_mode(OWN) == "builtin"


async def test_list_spools_spoolman_mode(respx_mock):
    respx_mock.get(f"{API}/spoolman/status").respond(200, json=load("spoolman_status.json"))
    route = respx_mock.get(SPOOLMAN_PATH).respond(200, json=load("spools_spoolman.json"))
    trap = _trap(respx_mock, BUILTIN_PATH)
    spools = await client().list_spools()
    assert route.called and not trap.called
    raw = load("spools_spoolman.json")[0]
    first = spools[0]
    assert first.id == raw["id"]
    assert first.remaining_g == raw["label_weight"] - raw["weight_used"]
    assert first.color_hex == "#000000"
    assert first.archived is False
    assert first.label_g == 1000
    assert (first.material, first.subtype, first.brand, first.color_name) == (
        "PLA", "Basic", "Example Brand", "Color 1")
    assert len(spools) == len(load("spools_spoolman.json"))


async def test_list_spools_builtin_mode(respx_mock):
    respx_mock.get(f"{API}/spoolman/status").respond(200, json=OWN)
    route = respx_mock.get(BUILTIN_PATH).respond(200, json=load("spools_builtin.json"))
    trap = _trap(respx_mock, SPOOLMAN_PATH)
    spools = await client().list_spools()
    assert route.called and not trap.called
    raw = load("spools_builtin.json")[0]
    first = spools[0]
    assert first.id == raw["id"]
    assert first.remaining_g == raw["label_weight"] - raw["weight_used"]
    assert first.color_hex == "#000000"
    assert first.archived is False


async def test_spoolman_unreachable_no_fallback(respx_mock):
    status = dict(load("spoolman_status.json"), connected=False)
    respx_mock.get(f"{API}/spoolman/status").respond(200, json=status)
    respx_mock.get(SPOOLMAN_PATH).respond(503, json={"detail": "Spoolman server is not reachable"})
    trap = _trap(respx_mock, BUILTIN_PATH)
    with pytest.raises(BambuddyUnavailable):
        await client().list_spools()
    assert not trap.called


async def test_spool_mode_error_reads_no_list(respx_mock):
    respx_mock.get(f"{API}/spoolman/status").respond(403, json={"detail": "Missing permission"})
    t1 = _trap(respx_mock, SPOOLMAN_PATH)
    t2 = _trap(respx_mock, BUILTIN_PATH)
    with pytest.raises(BambuddyRejected):
        await client().list_spools()
    assert not t1.called and not t2.called


def test_one_parser_for_both_lists():
    for name in ("spools_spoolman.json", "spools_builtin.json"):
        spools = parse_spools(load(name))
        assert spools and all(isinstance(s, Spool) for s in spools)
        for s in spools:
            assert s.created_at is None or (isinstance(s.created_at, datetime) and s.created_at.tzinfo is None)
    assert parse_spools(load("spools_spoolman.json"))[0].created_at == datetime(2026, 9, 14, 8, 30, 12)
    assert parse_spools(load("spools_builtin.json"))[0].created_at == datetime(2026, 4, 2, 10, 15, 41)


def test_spool_remaining_never_negative():
    data = load("spools_builtin.json")
    data[0]["weight_used"] = data[0]["label_weight"] + 50
    assert parse_spools(data)[0].remaining_g == 0


def test_spool_archived_and_missing_created_at():
    data = load("spools_spoolman.json")
    data[0]["archived_at"] = "2026-11-03T15:02:09Z"
    data[0]["created_at"] = None
    first = parse_spools(data)[0]
    assert first.archived is True and first.created_at is None


def test_spool_color_missing_or_malformed_is_none():
    data = load("spools_builtin.json")
    data[0]["rgba"] = None
    data[1]["rgba"] = "zz"
    spools = parse_spools(data)
    assert spools[0].color_hex is None and spools[1].color_hex is None


@pytest.mark.parametrize(
    "rgba, shown",
    [  # Bambuddy always sends eight digits (`_spoolman_helpers.py`, an opaque FF is appended to six)
        ("000000FF", "#000000"),  # opaque: as before, no alpha part
        ("ff0000ff", "#FF0000"),
        ("00000000", "#00000000"),  # transparent (Spec 9.1): the alpha part stays
        ("FFFFFF00", "#FFFFFF00"),
        ("00000080", "#00000080"),  # partly transparent
        ("#000000", "#000000"),
    ],
)
def test_spool_colour_keeps_an_alpha_that_is_not_opaque(rgba, shown):
    data = load("spools_builtin.json")
    data[0]["rgba"] = rgba
    assert parse_spools(data)[0].color_hex == shown


def test_a_transparent_spool_from_the_parser_is_a_sort_of_its_own(db):  # Spec 9.1
    from reprintbuddy.domain import filament

    data = load("spools_builtin.json")
    data[0]["rgba"], data[1]["rgba"] = "000000FF", "00000000"
    data[1]["material"], data[1]["subtype"] = data[0]["material"], data[0]["subtype"]
    black, clear = parse_spools(data)[:2]
    assert (black.material, black.subtype) == (clear.material, clear.subtype)
    with db.begin() as s:
        ids = filament.sync_sorts(s, [black, clear])
        assert ids[black.id] != ids[clear.id]


@pytest.mark.parametrize("key", ["id", "material", "label_weight", "weight_used"])
def test_spool_required_fields(key):
    data = load("spools_builtin.json")
    del data[0][key]
    with pytest.raises(BambuddyUnexpected):
        parse_spools(data)


def test_spool_unexpected_forms():
    with pytest.raises(BambuddyUnexpected):
        parse_spools({})
    with pytest.raises(BambuddyUnexpected):
        parse_spools([1])
    data = load("spools_builtin.json")
    data[0]["weight_used"] = "a lot"
    with pytest.raises(BambuddyUnexpected):
        parse_spools(data)
    data = load("spools_builtin.json")
    data[0]["created_at"] = "yesterday"
    with pytest.raises(BambuddyUnexpected):
        parse_spools(data)


@pytest.mark.parametrize("path, status", [(SPOOLMAN_PATH, load("spoolman_status.json")), (BUILTIN_PATH, OWN)])
async def test_spools_error_passes_through(respx_mock, path, status):
    respx_mock.get(f"{API}/spoolman/status").respond(200, json=status)
    route = respx_mock.get(path).respond(503, json={"detail": "down"})
    with pytest.raises(BambuddyUnavailable):
        await client().list_spools()
    route.respond(400, json={"detail": "bad"})
    with pytest.raises(BambuddyRejected) as info:
        await client().list_spools()
    assert info.value.detail == "bad"


def test_synthesized_color_name_is_dropped():
    # Spoolman mode fills color_name from the subtype when none is stored and flags it
    # (`_spoolman_helpers.py` `color_name_is_synthesized`); B6 must not see it as a colour.
    spools = {s.id: s for s in parse_spools(load("spools_spoolman.json"))}
    assert spools[9].color_name is None
    assert spools[9].subtype == "Basic"
    assert spools[9].color_hex == "#808080"  # the colour value itself is unaffected
    assert spools[4].color_name == "Color 1"
    assert spools[7].color_name == "Color 2"


def test_color_name_kept_without_the_flag():
    data = load("spools_builtin.json")
    assert all("color_name_is_synthesized" not in e for e in data)
    expected = [e.get("color_name") or None for e in data]
    assert [s.color_name for s in parse_spools(data)] == expected
    assert any(expected)


@pytest.mark.parametrize("flag", [False, None, "true", 1, "yes"])
def test_color_name_flag_other_than_true_means_not_synthesized(flag):
    data = load("spools_spoolman.json")
    data[2]["color_name_is_synthesized"] = flag
    assert parse_spools(data)[2].color_name == "Basic"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_float_is_unexpected(bad):
    # json.loads accepts these non-standard literals; they must not become numbers.
    import json

    data = load("spools_builtin.json")
    data[0]["weight_used"] = json.loads(f'{{"v": {bad}}}')["v"]
    with pytest.raises(BambuddyUnexpected):
        parse_spools(data)


@pytest.mark.parametrize("status,path", [("spoolman_status.json", SPOOLMAN_PATH), (None, BUILTIN_PATH)])
async def test_list_spools_asks_for_archived_spools_too(respx_mock, status, path):
    """Archived spools count when an old filament sort is converted (v0.3.7); Bambuddy leaves them
    out unless asked (`include_archived`, both lists)."""
    respx_mock.get(f"{API}/spoolman/status").respond(200, json=load(status) if status else OWN)
    route = respx_mock.get(path, params={"include_archived": "true"}).respond(200, json=[])
    await client().list_spools()
    assert route.called
