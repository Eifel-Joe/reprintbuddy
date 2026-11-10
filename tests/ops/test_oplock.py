"""The shared lock of update and restore (p91): whoever starts second, by hand or from the page, refuses.

The lock is a kernel lock on a file in the data folder, so it ends with the process that holds it,
also after a crash or a kill; the file that stays behind is no lock.
"""

import subprocess
import sys
import time

import pytest

from reprintbuddy.ops import oplock


def test_a_second_taker_is_refused_and_learns_what_runs(tmp_path):
    held = oplock.acquire(tmp_path, "update")
    try:
        assert oplock.holder(tmp_path) == "update"
        with pytest.raises(oplock.Held) as refused:
            oplock.acquire(tmp_path, "restore")
        assert refused.value.what == "update"
    finally:
        held.release()
    assert oplock.holder(tmp_path) is None
    oplock.acquire(tmp_path, "restore").release()


def test_no_file_is_no_lock(tmp_path):
    assert oplock.holder(tmp_path) is None
    assert not (tmp_path / oplock.LOCK_NAME).exists()  # asking creates nothing


def test_a_file_left_behind_is_no_lock(tmp_path):
    (tmp_path / oplock.LOCK_NAME).write_text('{"what": "update", "pid": 99999}')
    assert oplock.holder(tmp_path) is None
    oplock.acquire(tmp_path, "restore").release()


def test_release_twice_is_harmless(tmp_path):
    held = oplock.acquire(tmp_path, "restore")
    held.release()
    held.release()
    assert oplock.holder(tmp_path) is None


def test_the_lock_of_a_killed_process_is_gone(tmp_path):
    """A crash leaves no stale lock: the kernel drops it with the process."""
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from reprintbuddy.ops import oplock\n"
        "oplock.acquire(Path(sys.argv[1]), 'update')\n"
        "print('held', flush=True)\n"
        "time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        assert oplock.holder(tmp_path) == "update"
    finally:
        child.kill()
        child.wait(timeout=10)
        child.stdout.close()
    for _ in range(50):  # the kernel needs a moment on some systems
        if oplock.holder(tmp_path) is None:
            break
        time.sleep(0.05)
    assert oplock.holder(tmp_path) is None
    oplock.acquire(tmp_path, "restore").release()
