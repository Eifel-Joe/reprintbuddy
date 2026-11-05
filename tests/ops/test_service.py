from reprintbuddy.ops.service import RESTART_CMD, START_UPDATE_CMD, request_restart, start_update_unit


def test_request_restart_command(fake_runner):
    request_restart(fake_runner)
    assert fake_runner.calls == [RESTART_CMD]


def test_start_update_command(fake_runner):
    start_update_unit(fake_runner)
    assert fake_runner.calls == [START_UPDATE_CMD]


def test_fake_runner_longest_prefix_and_repeat(fake_runner):
    from reprintbuddy.ops.runner import CommandResult

    fake_runner.on(["git"], CommandResult(1))
    fake_runner.on(["git", "tag"], CommandResult(0, "a"), CommandResult(0, "b"))
    assert fake_runner(["git", "status"]).returncode == 1
    assert fake_runner(["git", "tag", "-l"]).stdout == "a"
    assert fake_runner(["git", "tag"]).stdout == "b"
    assert fake_runner(["git", "tag"]).stdout == "b"
    assert fake_runner(["other"]).returncode == 0
