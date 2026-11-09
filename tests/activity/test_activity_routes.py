"""Every request that changes something writes a line of the activity log or belongs to the journal
(Spec 10.7 "Tests prüfen, dass jede Anfrage, die etwas ändert, entweder einen Eintrag schreibt oder als
Buchung ins Journal gehört"). A new route fails here until it is put into one of the lists; the tests of
`tests/activity` show the lines themselves.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import api_routes, make_client

WRITES = {"POST", "PUT", "PATCH", "DELETE"}

# Writes a line: through the capture of settings, account and master data, or through `activity.record`.
LOGGED = {
    "/articles/new/file/{file_id:int}/plate/{index:int}": "test_a_new_article_is_created_by_the_user",
    "/articles/new/upload": "test_an_upload_names_file_and_folder",
    "/articles/{article_id:int}/archive": "test_an_article_archived_and_restored_by_the_user",
    "/articles/{article_id:int}/auto": "test_the_automation_of_an_article_switched_in_the_list",
    "/articles/{article_id:int}/edit": "test_an_article_changed_on_its_edit_page",
    "/articles/{article_id:int}/filaments": "test_the_filaments_of_an_article_saved_on_its_filament_page",
    "/articles/{article_id:int}/label": "test_a_test_label_and_an_article_label",
    # restored with another plate
    "/articles/{article_id:int}/relink/file/{file_id:int}/plate/{index:int}": "test_an_archived_article_restored_with_another_plate",
    "/articles/{article_id:int}/restore": "test_an_article_archived_and_restored_by_the_user",
    "/articles/{article_id:int}/target": "test_the_target_quantity_saved_in_the_list",
    "/filament/{sort_id:int}": "test_the_order_settings_of_a_spool_sort",
    "/login": "test_signed_in_and_out_with_the_address",
    "/logout": "test_signed_in_and_out_with_the_address",
    "/parts/new": "test_a_part_created_changed_archived_and_restored",
    "/parts/{part_id:int}/archive": "test_a_part_created_changed_archived_and_restored",
    "/parts/{part_id:int}/edit": "test_a_part_created_changed_archived_and_restored",
    "/parts/{part_id:int}/restore": "test_a_part_created_changed_archived_and_restored",
    "/reorder/mark": "test_a_reorder_marked_and_received_by_hand",
    "/reorder/marks/{mark_id:int}/received": "test_a_reorder_marked_and_received_by_hand",
    "/sets/new": "test_a_set_created_changed_archived_and_restored",
    "/sets/{set_id:int}/archive": "test_a_set_created_changed_archived_and_restored",
    "/sets/{set_id:int}/edit": "test_a_set_created_changed_archived_and_restored",
    "/sets/{set_id:int}/restore": "test_a_set_created_changed_archived_and_restored",
    "/settings/automation": "test_the_automation_settings_saved_on_the_page",
    "/settings/backup": "test_a_full_backup_says_whether_it_is_encrypted",
    "/settings/bambuddy": "test_secrets_of_the_settings_pages_never_reach_log_page_or_export",
    "/settings/labels": "test_the_label_settings_saved_on_the_page",
    "/settings/labels/test": "test_a_test_label_and_an_article_label",
    "/settings/language": "test_a_setting_saved_on_the_page_is_the_user",
    "/settings/notify": "test_secrets_of_the_settings_pages_never_reach_log_page_or_export",
    "/settings/notify/test": "test_the_test_message_is_the_user",
    "/settings/password": "test_a_new_password_is_logged_with_the_address",
    "/settings/qr": "test_the_address_in_the_qr_codes_saved_on_the_page",
    "/settings/restart": "test_a_restart_that_was_asked_for",
    "/settings/restore": "test_a_done_restore_is_logged_by_the_next_start_into_the_restored_database",
    "/settings/restore/cancel": "test_a_cancelled_restore",
    # a refused file; an accepted one waits for the confirmation
    "/settings/restore/upload": "test_a_foreign_file_is_refused_with_its_reason",
    "/settings/server": "test_secrets_of_the_settings_pages_never_reach_log_page_or_export",
    "/settings/update": "test_the_new_version_once_and_the_start_of_an_update_with_its_result",
    "/settings/update/auto": "test_the_automatic_check_for_versions_switched_on_the_page",
    # a newer version, once
    "/settings/update/check": "test_the_new_version_once_and_the_start_of_an_update_with_its_result",
    "/setup": "test_the_setup_logs_the_account_with_the_address",
    "/setup/bambuddy/connect": "test_the_bambuddy_connection_of_the_setup",
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
    assert not set(LOGGED) & JOURNAL and not set(LOGGED) & set(EXEMPT) and not JOURNAL & set(EXEMPT)
    known = set(LOGGED) | JOURNAL | set(EXEMPT)
    assert paths - known == set(), "new write routes: put each into LOGGED, JOURNAL or EXEMPT"
    assert known - paths == set(), "routes that no longer exist"


def test_every_logged_route_names_a_test_that_exists():
    """The test named for a route shows its line; a name that is not a test in `tests/activity` fails."""
    defined = set()
    for source in Path(__file__).parent.glob("*.py"):
        defined |= set(re.findall(r"^(?:async )?def (test_\w+)\(", source.read_text(encoding="utf-8"), re.M))
    missing = {path: name for path, name in LOGGED.items() if name not in defined}
    assert missing == {}, "routes whose test does not exist"
