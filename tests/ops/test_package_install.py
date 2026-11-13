"""v0.4.9 p500: an installation from the package runs without the folder it was installed from.

pip installs the unpacked package (`releases/<tag>`) into the environment, not as an editable link. After
the first package update the old git copy goes (Spec 11.3a), and older unpacked versions go too; so the
templates, the static files, the language files and the migrations must all be inside the environment.
This test does what the update does: install from an unpacked copy of this code, take the copy away,
then start the app from what pip installed and ask for a page, a static file and the German texts.

pip builds offline here (`--no-build-isolation`, setuptools from the dev extras); nothing is fetched.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import reprintbuddy
from reprintbuddy.i18n import load_locales
from reprintbuddy.web.app import create_app
from starlette.testclient import TestClient

app = create_app(Path(sys.argv[2]), run_engine=False)
client = TestClient(app, follow_redirects=False)
setup = client.get("/setup")
css = client.get("/static/css/reprintbuddy.css")
print(json.dumps({
    "module": reprintbuddy.__file__,
    "setup": [setup.status_code, "<form" in setup.text],
    "css": [css.status_code, len(css.content)],
    "german": load_locales()["de"].get("ops.update.reason.package_missing", ""),
}))
'''


def test_the_installed_package_runs_without_its_source_folder(tmp_path):
    source = tmp_path / "opt" / "releases" / "v0.0.0"
    source.mkdir(parents=True)
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(ROOT / name, source / name)
    shutil.copytree(ROOT / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    target = tmp_path / "venv-site"
    built = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--no-cache-dir",
         "--disable-pip-version-check", "--target", str(target), str(source)],
        capture_output=True, text=True, timeout=300,
    )
    assert built.returncode == 0, built.stderr[-2000:]

    shutil.rmtree(tmp_path / "opt")  # the unpacked package is gone, as after the clean-up of an update
    assert not source.exists()

    probe = tmp_path / "probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    ran = subprocess.run(
        [sys.executable, str(probe), str(target), str(tmp_path / "data.db")],
        capture_output=True, text=True, timeout=120, cwd=tmp_path,
    )
    assert ran.returncode == 0, ran.stderr[-3000:]
    found = json.loads(ran.stdout.strip().splitlines()[-1])
    assert Path(found["module"]).resolve().is_relative_to(target.resolve()), found["module"]
    assert found["setup"] == [200, True]  # a page: templates came along
    assert found["css"][0] == 200 and found["css"][1] > 1000  # a static file
    assert found["german"].startswith("Das Paket der neuen Version")  # a language file
