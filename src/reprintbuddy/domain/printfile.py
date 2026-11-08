"""Checks for a print file uploaded through Reprintbuddy (Spec 5.1a, v0.3.9 p20).

Reprintbuddy cannot delete a file once it is in Bambuddy, so everything is checked before the
file is sent. The functions here are pure: no request, no Bambuddy.
"""

from __future__ import annotations

from collections.abc import Iterable

from reprintbuddy.bambuddy.types import LibraryFolder


def is_external_folder(folders: Iterable[LibraryFolder], folder_id: int | None) -> bool:
    """True when the folder or any folder above it is external (`is_external`).

    Bambuddy marks the sub-folders its scan creates as external too; walking up also covers
    an internal folder hung below an external one. The top level is never external. An
    unknown id is not external here; that it must exist is checked where it is used.
    """
    by_id = {folder.id: folder for folder in folders}
    seen: set[int] = set()
    current = by_id.get(folder_id) if folder_id is not None else None
    while current is not None and current.id not in seen:
        if current.is_external:
            return True
        seen.add(current.id)
        current = by_id.get(current.parent_id) if current.parent_id is not None else None
    return False
