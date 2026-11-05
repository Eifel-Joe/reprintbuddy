import sys

from reprintbuddy.ops.runner import CommandResult, subprocess_runner


def test_runner_success():
    result = subprocess_runner([sys.executable, "-c", "print('hi')"])
    assert result.returncode == 0
    assert result.stdout.replace("\r\n", "\n") == "hi\n"
    assert result.ok


def test_runner_nonzero():
    result = subprocess_runner([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert result.returncode == 3
    assert not result.ok


def test_runner_missing_program():
    assert subprocess_runner(["definitely-not-a-program-xyz"]).returncode == 127


def test_runner_timeout():
    result = subprocess_runner(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5
    )
    assert result.returncode == 124


def test_runner_disables_git_prompt():
    code = "import os; print(os.environ['GIT_TERMINAL_PROMPT'])"
    result = subprocess_runner([sys.executable, "-c", code])
    assert result.stdout.strip() == "0"


def test_command_result_is_frozen():
    import dataclasses

    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        CommandResult(0).returncode = 1  # type: ignore[misc]
