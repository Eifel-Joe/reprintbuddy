"""The fixed list of events of the activity log (Spec 10.7): event kind -> group of the page.

A new event is added here, with its texts `activity.kind.<kind>` in both language files (a test
checks that). `ACTORS` are the four answers to "who" (Spec 10.7 "Von wem").
"""

from __future__ import annotations

from typing import Literal

Actor = Literal["user", "auto", "bambuddy", "system"]
ACTORS: tuple[Actor, ...] = ("user", "auto", "bambuddy", "system")

# The groups in the order of Spec 10.7, which is also the order of the filter buttons.
AREAS: tuple[str, ...] = ("login", "settings", "master", "ops", "bambuddy", "messages", "labels")

KINDS: dict[str, str] = {
    # 1. sign-in
    "login": "login",
    "logout": "login",
    "login_failed": "login",
    "account_created": "login",
    "credentials_changed": "login",
    # 2. settings
    "settings_changed": "settings",
    # 3. master data
    "article_created": "master",
    "article_changed": "master",
    "article_target": "master",
    "article_auto": "master",
    "article_archived": "master",
    "article_restored": "master",
    "part_created": "master",
    "part_changed": "master",
    "part_archived": "master",
    "part_restored": "master",
    "set_created": "master",
    "set_changed": "master",
    "set_archived": "master",
    "set_restored": "master",
    "set_migrated": "master",  # the update to v0.3.18 rebuilt a set (Spec 5.4, p340)
    "filament_settings_changed": "master",
    "reorder_ordered": "master",
    "reorder_received": "master",
    "reorder_withdrawn": "master",
    # 4. backup and update
    "backup_downloaded": "ops",
    "export_downloaded": "ops",
    "restore_rejected": "ops",
    "restore_cancelled": "ops",
    "restore_done": "ops",
    "update_started": "ops",
    "update_result": "ops",
    "version_found": "ops",
    "restart_requested": "ops",
    "started": "ops",
    # 5. Bambuddy
    "bambuddy_connection": "bambuddy",
    "bambuddy_rights": "bambuddy",
    "upload": "bambuddy",
    # 6. messages
    "message_sent": "messages",
    "message_test": "messages",
    # 7. labels
    "label_printed": "labels",
    "label_test": "labels",
}


def area_of(kind: str) -> str:
    """The group of `kind`; an unknown kind is a programming error."""
    return KINDS[kind]
