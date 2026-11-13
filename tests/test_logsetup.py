"""p166: the service sets up its own log, so INFO lines of Reprintbuddy reach the system log.

Without a setup Python writes only WARNING and above (`logging.lastResort`). The setup is
checked in a fresh interpreter: pytest puts handlers of its own on the root logger.
"""

import subprocess
import sys

from reprintbuddy import cli
from reprintbuddy.web import server as web_server

SCRIPT = """
import logging
from reprintbuddy.logsetup import configure_logging
configure_logging()
logging.getLogger("reprintbuddy.some.module").info("own info line")
logging.getLogger("reprintbuddy.some.module").debug("own debug line")
logging.getLogger("httpx").info("foreign info line")
logging.getLogger("httpx").warning("foreign warning line")
"""


def run_script(script: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)


def test_info_of_reprintbuddy_reaches_stderr_with_level_and_name():
    result = run_script(SCRIPT)
    assert result.returncode == 0, result.stderr
    assert "INFO reprintbuddy.some.module: own info line" in result.stderr


def test_debug_and_info_of_other_libraries_stay_out():
    # Libraries may log addresses with tokens at INFO (an HTTP client logs every request line).
    result = run_script(SCRIPT)
    assert "own debug line" not in result.stderr
    assert "foreign info line" not in result.stderr
    assert "WARNING httpx: foreign warning line" in result.stderr


def test_a_second_call_does_not_double_the_lines():
    result = run_script(SCRIPT.replace("configure_logging()", "configure_logging(); configure_logging()"))
    assert result.stderr.count("own info line") == 1


def test_serve_sets_up_the_log_before_the_server_runs(tmp_path, monkeypatch):
    order = []
    monkeypatch.setattr(cli, "configure_logging", lambda: order.append("log"))
    monkeypatch.setattr(web_server, "serve", lambda app, **kw: order.append("run"))
    assert cli.main(["serve", "--db", str(tmp_path / "t.db")]) == 0
    assert order == ["log", "run"]
