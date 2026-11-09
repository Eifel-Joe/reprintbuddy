"""Every request that changes something writes a line of the activity log or belongs to the journal
(Spec 10.7 "Tests prüfen, dass jede Anfrage, die etwas ändert, entweder einen Eintrag schreibt oder als
Buchung ins Journal gehört"). A new route fails here until it is put into one of the lists; the tests of
`tests/activity` show the lines themselves.
"""

from __future__ import annotations

from conftest import api_routes, make_client

WRITES = {"POST", "PUT", "PATCH", "DELETE"}

# Writes a line: through the capture of settings, account and master data, or through `activity.record`.
LOGGED = {
    "/articles/new/file/{file_id:int}/plate/{index:int}",
    "/articles/new/upload",
    "/articles/{article_id:int}/archive",
    "/articles/{article_id:int}/auto",
    "/articles/{article_id:int}/edit",
    "/articles/{article_id:int}/filaments",
    "/articles/{article_id:int}/label",
    "/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}",  # restored with another plate
    "/articles/{article_id:int}/restore",
    "/articles/{article_id:int}/target",
    "/filament/{sort_id:int}",
    "/login",
    "/logout",
    "/parts/new",
    "/parts/{part_id:int}/archive",
    "/parts/{part_id:int}/edit",
    "/parts/{part_id:int}/restore",
    "/reorder/mark",
    "/reorder/marks/{mark_id:int}/received",
    "/sets/new",
    "/sets/{set_id:int}/archive",
    "/sets/{set_id:int}/edit",
    "/sets/{set_id:int}/restore",
    "/settings/automation",
    "/settings/backup",
    "/settings/bambuddy",
    "/settings/labels",
    "/settings/labels/test",
    "/settings/language",
    "/settings/notify",
    "/settings/notify/test",
    "/settings/password",
    "/settings/qr",
    "/settings/restart",
    "/settings/restore",
    "/settings/restore/cancel",
    "/settings/restore/upload",  # a refused file; an accepted one waits for the confirmation
    "/settings/server",
    "/settings/update",
    "/settings/update/auto",
    "/settings/update/check",  # a newer version, once
    "/setup",
    "/setup/bambuddy/connect",
}

# Bookings, the queue, prints and pauses: the journal's (Spec 6.3).
JOURNAL = {
    "/api/inspections/{inspection_id:int}",
    "/articles/{article_id:int}/confirm-file",
    "/articles/{article_id:int}/resume",
    "/assembly/{article_id:int}",
    "/bookings/inventory",
    "/bookings/part-inventory",
    "/bookings/part-receipt",
    "/bookings/sale",
    "/bookings/stock-in",
    "/inspection/all-good",
    "/inspection/{inspection_id:int}",
    "/orders/new",
    "/orders/{order_id:int}/cancel",
    "/orders/{order_id:int}/ship",
    "/queue/manual",
    "/queue/{entry_id:int}/delete",
    "/queue/{entry_id:int}/hold",
    "/queue/{entry_id:int}/move",  # the order of the queue: no journal line, but the queue's
    "/queue/{entry_id:int}/prints",
    "/queue/{entry_id:int}/release",
}

# Change nothing that is kept; why.
EXEMPT = {
    "/queue/poll": "asks Bambuddy now; what changes is logged by the poll (connection) or the journal",
    "/bambuddy/rights/recheck": "checks the key again; a change of the rights is logged after the next poll",
    "/bambuddy/rights/later": "hides a notice in this browser",
    "/bambuddy/rights/done": "hides a notice in this browser",
}


def test_every_write_route_is_logged_journaled_or_exempt(tmp_path):
    app = make_client(tmp_path / "routes.db").app
    paths = {route.path for route in api_routes(app) if route.methods & WRITES}
    assert not LOGGED & JOURNAL and not LOGGED & set(EXEMPT) and not JOURNAL & set(EXEMPT)
    known = LOGGED | JOURNAL | set(EXEMPT)
    assert paths - known == set(), "new write routes: put each into LOGGED, JOURNAL or EXEMPT"
    assert known - paths == set(), "routes that no longer exist"
