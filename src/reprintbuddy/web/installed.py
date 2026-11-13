"""The installed version as this process knows it, for the update card, the bar of every page and the
start of an update (p543, v0.4.11).

The version is read once per process (`git describe` for an installation made with git, v0.3.8 p96), and
the result of the check names it too (`VersionInfo.installed`). Since v0.4.9 the process can start before
the version it runs is written down: the update helper restarts the service, waits until it answers and
only then writes `release.json` (`updater._settle`). The new process may read the installed version, or
finish its first check, in between. So a remembered version counts only as long as `release.json` is the
file it was read from (`marker_stamp`): a new file is read again, and a result of the check that names
another installed version is dropped, as after a new choice of versions.

Rejected: reading `release.json` on every page view without remembering (the same file read, but a
broken file would turn into a `git describe` in the event loop); a reload of the page by its script
(the page already reloads when the state of the update changes, "succeeded" comes after `_settle`);
recomputing "update available" against the new version (the next check does that, with the notes).
"""

from __future__ import annotations

import logging
from typing import Any

from reprintbuddy.ops.installation import marker_stamp, marker_version

log = logging.getLogger(__name__)


def refresh(state: Any) -> None:
    """Bring a remembered installed version up to date with `release.json`. Cheap (one `stat`; one small
    file read after a change) and never runs git, so it may run in the event loop on every page."""
    if not hasattr(state, "installed_tag"):
        return  # nothing remembered: the next reader reads it fresh
    install_dir = state.ops.install_dir
    stamp = marker_stamp(install_dir)
    if stamp is None or stamp == getattr(state, "installed_stamp", None):
        return  # no file (git, or no installation): the version of the process stays; or the same file
    readable, tag = marker_version(install_dir)
    if not readable:
        return  # gone or broken in between: asked again on the next view
    state.installed_stamp = stamp
    state.installed_tag = tag
    info = getattr(state, "version_info", None)
    if info is not None and info.installed != tag:
        state.version_info = None
        log.info("update: the installed version is now %s; the result of the last check is dropped", tag)
