"""p644: a restart the service asks for itself is not a failure when its own sudo is stopped with it.

systemd stops the whole control group of the unit with SIGTERM (systemd.kill(5), KillMode=control-group by
default), the `sudo systemctl restart --no-block` the service started included; sudo relays the signal and
then sends it to itself (sudo(8), "Signal handling"). The runner then reports the command as ended by SIGTERM
(a negative return code, nothing on stderr) although the restart is under way.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from conftest import csrf_of

from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import RESTART_CMD
from reprintbuddy.web.routes import backup

STOPPED_WITH_THE_SERVICE = CommandResult(-int(signal.SIGTERM), "", "")


def test_the_restart_from_the_replace_worker_stopped_with_the_service_is_no_failure(caplog):
    caplog.set_level(logging.INFO, logger="reprintbuddy")
    backup._restart_from_worker(lambda args, **_: STOPPED_WITH_THE_SERVICE)
    assert "restart after restore failed" not in caplog.text


def test_the_restart_task_stopped_with_the_service_is_no_failure(caplog):
    caplog.set_level(logging.INFO, logger="reprintbuddy")

    async def run():
        await backup._start_restart(lambda args, **_: STOPPED_WITH_THE_SERVICE)
        await asyncio.sleep(0)  # the done callback

    asyncio.run(run())
    assert "restart after restore failed" not in caplog.text


def test_a_restart_that_really_failed_is_still_logged(caplog):
    backup._restart_from_worker(lambda args, **_: CommandResult(1, "", "sudo: a password is required"))
    assert "restart after restore failed: sudo: a password is required" in caplog.text


def test_the_restart_button_stopped_with_the_service_shows_the_restart_page(ops_env, caplog):
    ops_env.runner.on(RESTART_CMD, STOPPED_WITH_THE_SERVICE)
    token = csrf_of(ops_env.client, "/settings")
    response = ops_env.client.post("/settings/restart", data={"csrf_token": token})
    assert response.status_code == 200
    assert "Restarting" in response.text
    assert "restart failed" not in caplog.text


def test_only_sigterm_counts_as_under_way():
    from reprintbuddy.ops.service import restart_under_way

    assert restart_under_way(CommandResult(0))
    assert restart_under_way(STOPPED_WITH_THE_SERVICE)
    assert not restart_under_way(CommandResult(-int(signal.SIGKILL) if hasattr(signal, "SIGKILL") else -9))
    assert not restart_under_way(CommandResult(1, "", "sudo: a password is required"))
    assert not restart_under_way(CommandResult(124, "", "timed out after 15 s"))
    assert not restart_under_way(CommandResult(130, "", "cancelled"))


def test_the_restored_page_shows_the_restart_under_way_when_our_sudo_is_stopped_with_the_service(ops_env, fast_kdf):
    """Review finding: the page after a restore took the result's `ok` and showed "could not restart"."""
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "_restart_backup_helpers", pathlib.Path(__file__).resolve().parent / "test_ops_full_backup.py"
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    content = helpers.made_backup(ops_env)
    ops_env.runner.on(RESTART_CMD, STOPPED_WITH_THE_SERVICE)
    helpers.upload(ops_env, content)
    response = helpers.confirm(ops_env)
    assert response.status_code == 200
    assert "Restart Reprintbuddy now" not in response.text
    assert 'data-health-url="/healthz"' in response.text
