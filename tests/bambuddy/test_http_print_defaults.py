"""Bambuddy's default print options (p342, 0.4.0): read from the settings, sent with each entry.

Bambuddy's `add_to_queue` does not apply its "Default print options" setting to a
`POST /queue/` without options; it takes the fixed defaults of `PrintQueueItemCreate`
(`schemas/print_queue.py`). Its own "Add to queue" reads them from the settings
(`services/print_defaults.py`). Reprintbuddy does the same: it reads
`GET /settings/` and sends the six fields with every entry.
"""

import json

import pytest
from fixtures import API, client, load

from reprintbuddy.bambuddy.client import BambuddyRejected, BambuddyUnavailable
from reprintbuddy.bambuddy.types import EnqueueRequest, PrintOptions

CURRENT = {
    "default_bed_levelling": "on",
    "default_flow_cali": "on",
    "default_nozzle_offset_cali": "auto",
    "default_vibration_cali": False,
    "default_layer_inspect": True,
    "default_timelapse": False,
    "language": "de",  # every other setting is ignored
}


def _settings(respx_mock, answer):
    return respx_mock.get(f"{API}/settings/").respond(200, json=answer)


async def test_reads_the_six_defaults(respx_mock):
    route = _settings(respx_mock, CURRENT)
    options = await client().print_defaults()
    assert route.call_count == 1
    assert options == PrintOptions(
        bed_levelling="on",
        flow_cali="on",
        nozzle_offset_cali="auto",
        vibration_cali=False,
        layer_inspect=True,
        timelapse=False,
    )


async def test_tri_state_text_is_normalised(respx_mock):
    _settings(respx_mock, {**CURRENT, "default_bed_levelling": " ON ", "default_flow_cali": "Off"})
    options = await client().print_defaults()
    assert (options.bed_levelling, options.flow_cali) == ("on", "off")


async def test_older_bambuddy_with_booleans_keeps_booleans(respx_mock):
    # Before Bambuddy v1.2.5 the three were booleans in the settings and in the queue alike;
    # the nozzle offset calibration came later still.
    old = {
        "default_bed_levelling": True,
        "default_flow_cali": False,
        "default_vibration_cali": True,
        "default_layer_inspect": False,
        "default_timelapse": True,
    }
    _settings(respx_mock, old)
    options = await client().print_defaults()
    assert options == PrintOptions(
        bed_levelling=True,
        flow_cali=False,
        nozzle_offset_cali=None,
        vibration_cali=True,
        layer_inspect=False,
        timelapse=True,
    )


@pytest.mark.parametrize(
    "key, value",
    [
        ("default_bed_levelling", "maybe"),
        ("default_flow_cali", 1),
        ("default_nozzle_offset_cali", None),
        ("default_vibration_cali", "true"),
        ("default_timelapse", 0),
    ],
)
async def test_an_unknown_value_is_left_out(respx_mock, key, value):
    _settings(respx_mock, {**CURRENT, key: value})
    options = await client().print_defaults()
    assert key.removeprefix("default_") not in options.body()


async def test_settings_that_are_no_object_are_unexpected(respx_mock):
    _settings(respx_mock, [1, 2])
    with pytest.raises(BambuddyUnavailable):
        await client().print_defaults()


async def test_refused_settings_are_rejected(respx_mock):
    respx_mock.get(f"{API}/settings/").respond(403, json={"detail": "Missing permission: settings:read"})
    with pytest.raises(BambuddyRejected) as info:
        await client().print_defaults()
    assert info.value.status == 403


async def test_server_error_is_unavailable(respx_mock):
    respx_mock.get(f"{API}/settings/").respond(503)
    with pytest.raises(BambuddyUnavailable):
        await client().print_defaults()


def test_body_holds_only_the_known_values():
    assert PrintOptions().body() == {}
    assert PrintOptions(bed_levelling="on", timelapse=False).body() == {
        "bed_levelling": "on",
        "timelapse": False,
    }


async def test_enqueue_sends_the_options(respx_mock):
    route = respx_mock.post(f"{API}/queue/").respond(200, json=load("q4.json"))
    options = PrintOptions("on", "on", "auto", False, True, False)
    await client().enqueue(EnqueueRequest(5, 1, "A1", None, False, None, options))
    assert json.loads(route.calls.last.request.content) == {
        "library_file_id": 5,
        "plate_id": 1,
        "target_model": "A1",
        "manual_start": False,
        "bed_levelling": "on",
        "flow_cali": "on",
        "nozzle_offset_cali": "auto",
        "vibration_cali": False,
        "layer_inspect": True,
        "timelapse": False,
    }
