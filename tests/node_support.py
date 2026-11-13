"""Run a JavaScript test harness in node and read its JSON answer (refactoring before 1.0.0, R2).

The tests of the page scripts each had this as their own `result` fixture. Without node the test
is skipped, as before.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESSES = Path(__file__).parent / "web"
SCRIPTS = Path(__file__).parents[1] / "src" / "reprintbuddy" / "web" / "static" / "js"
NODE = shutil.which("node")


def run_harness(harness: str, *scripts: Path) -> dict:
    """`harness` is a file in tests/web; `scripts` are handed to it in this order."""
    if NODE is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [NODE, str(HARNESSES / harness), *(str(script) for script in scripts)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return json.loads(done.stdout)
