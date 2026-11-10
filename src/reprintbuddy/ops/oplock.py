"""One lock for update and restore (p91): only one of them runs, however it was started.

The page already refuses an update during its restore and the other way round, but it only knows
about the update unit. `reprintbuddy update` typed by hand in the container runs outside the unit,
and `reprintbuddy restore-backup` runs while the service is stopped; neither saw the other. Now each
of them takes this lock first and refuses when it is taken.

The lock is a kernel lock on the file `maintenance.lock` in the data folder (`flock`; on Windows,
where the tests run, `msvcrt.locking`). It ends with the process that holds it, also after a crash,
a kill or a power cut, so a stale lock cannot stay behind: the file that stays is no lock, and its
text (what runs, process id, since when) is only read while the lock is held. Rejected: a file whose
mere existence is the lock, with a process id to check - process ids are reused, and a container
restart leaves the file but no process.
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

# At the top, not in the functions: the update helper holds the lock while pip replaces the package.
if os.name == "nt":
    import msvcrt
else:
    import fcntl

LOCK_NAME = "maintenance.lock"
# Windows locks a byte range and refuses to read a locked range: lock a byte far past the text.
_WIN_OFFSET = 1 << 30


class Held(Exception):
    """The lock is taken; `what` says by what ("update", "restore" or "unknown")."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        os.lseek(fd, _WIN_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:  # BlockingIOError: taken
        return False
    return True


def _unlock(fd: int) -> None:
    if os.name == "nt":
        os.lseek(fd, _WIN_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


def _what(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        info = json.loads(os.read(fd, 4096).decode("utf-8"))
        what = info.get("what") if isinstance(info, dict) else None
    except (OSError, ValueError):
        what = None
    return what if what in ("update", "restore") else "unknown"


class OpLock:
    """A taken lock; `release` (or the end of the process) frees it."""

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            with contextlib.suppress(OSError):
                _unlock(fd)
        finally:
            os.close(fd)


def acquire(data_dir: Path, what: str) -> OpLock:
    """Take the lock for `what` ("update" or "restore"); `Held` if anybody has it."""
    fd = os.open(data_dir / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _try_lock(fd):
            raise Held(_what(fd))
        text = json.dumps({"what": what, "pid": os.getpid(), "since": datetime.now(UTC).isoformat()})
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, text.encode("utf-8"))
    except BaseException:
        os.close(fd)  # closing also drops a lock taken just before a failed write
        raise
    return OpLock(fd)


def holder(data_dir: Path) -> str | None:
    """What holds the lock ("update", "restore", "unknown"); None when nobody does. Creates nothing.

    It asks by trying the lock for a moment; a taker at that very moment is refused once (the page
    and the commands ask only while an update is recorded as running, see `running.update_running`).
    """
    try:
        fd = os.open(data_dir / LOCK_NAME, os.O_RDWR)
    except FileNotFoundError:
        return None
    try:
        if _try_lock(fd):
            _unlock(fd)
            return None
        return _what(fd)
    finally:
        os.close(fd)
