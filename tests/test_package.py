import reprintbuddy


def test_version():
    assert reprintbuddy.__version__ == "0.4.6"


def test_the_label_fonts_are_part_of_the_package():
    """Labels are drawn with DejaVu Sans (Spec 8.5): the two files and their license text have to
    travel with an installation made from the package (`pip install`), not only with a checkout."""
    import tomllib
    from importlib import resources
    from pathlib import Path

    root = Path(reprintbuddy.__file__).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "labels/fonts/*" in pyproject["tool"]["setuptools"]["package-data"]["reprintbuddy"]
    assert any(dep.lower().startswith("pillow") for dep in pyproject["project"]["dependencies"])
    fonts = resources.files("reprintbuddy") / "labels" / "fonts"
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "LICENSE-dejavu.txt"):
        assert (fonts / name).is_file(), name
    text = (fonts / "LICENSE-dejavu.txt").read_text(encoding="utf-8")
    assert "Bitstream" in text and "SHA-256" in text and "matplotlib" in text


def _floor(requirement: str) -> tuple[int, ...]:
    """The lowest version a requirement like `httpx>=0.28.1` allows, as a tuple of numbers."""
    import re

    found = re.search(r">=\s*([0-9][0-9.]*)", requirement)
    assert found, requirement
    return tuple(int(part) for part in found.group(1).split(".") if part)


def _requirement(name: str, entries: list[str]) -> str:
    matches = [entry for entry in entries if entry.lower().replace("=", ">").split(">")[0].strip() == name]
    assert len(matches) == 1, (name, matches)
    return matches[0]


def test_httpx_floor_is_the_version_the_shared_tls_context_was_checked_with():
    """`shared_tls_client_factory` leans on how httpx 0.28.1 builds and uses its default
    TLS context (v0.3.9 p77): an older version is not covered by the checks, so it is not allowed."""
    import tomllib
    from importlib import metadata
    from pathlib import Path

    root = Path(reprintbuddy.__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    floor = _floor(_requirement("httpx", project["dependencies"]))
    assert floor >= (0, 28, 1)
    from packaging.version import Version

    assert Version(metadata.version("httpx")) >= Version(".".join(str(part) for part in floor))


def test_respx_floor_supports_the_httpx_floor():
    """respx 0.21 patches httpx internals that 0.28 changed; the test dependency follows."""
    import tomllib
    from pathlib import Path

    root = Path(reprintbuddy.__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert _floor(_requirement("respx", project["optional-dependencies"]["dev"])) >= (0, 22)


def test_python_3_13_is_the_floor_everywhere():
    """v0.3.12 p163: Debian 13 (Python 3.13) is the oldest system the installer accepts. The package, the
    installer (`require_python` and the floor of `venv_usable`) and the CI matrix (3.13 and the next one, 3.14)
    name the same floor, so that they cannot drift apart again. (v0.3.9 p101 had set 3.12: below it sqlite3
    lacks `SQLITE_DBCONFIG_DEFENSIVE`; 3.13 has it too.)"""
    import re
    import tomllib
    from pathlib import Path

    root = Path(reprintbuddy.__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.13"
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = re.search(r"python:\s*\[([^\]]*)\]", workflow).group(1)
    assert [v.strip().strip("\"'") for v in versions.split(",")] == ["3.13", "3.14"]
    assert "3.12" not in workflow and "3.11" not in workflow
    script = (root / "install" / "install.sh").read_text(encoding="utf-8")
    assert "sys.version_info >= (3, 13)" in script  # require_python
    assert '"${minor}" -ge 13' in script  # venv_usable
    for old in ("(3, 12)", "-ge 12", "Python 3.12 or newer"):
        assert old not in script, old


def test_no_code_is_left_that_only_serves_python_3_11():
    from pathlib import Path

    source = (Path(reprintbuddy.__file__).parent / "ops" / "fullbackup.py").read_text(encoding="utf-8")
    assert 'getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"' not in source
    assert "on 3.11" not in source


def test_a_run_started_by_hand_names_its_branch_and_purpose():
    """p502: GitHub lists a run started with `gh workflow run ci.yml --ref release/vX.Y.Z` under the
    workflow name only. `run-name` (docs: "Workflow syntax", `run-name`) may use the `github` and `inputs`
    contexts; if it is empty or only whitespace GitHub uses the event's own title (the commit message of a
    push, the title of a pull request), so pushes and pull requests keep what they had. The run itself
    shows in `gh run list --workflow ci.yml` and is checked there, after the release branch has run."""
    from pathlib import Path

    import yaml

    root = Path(reprintbuddy.__file__).parents[2]
    text = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on` as True
    purpose = triggers["workflow_dispatch"]["inputs"]["purpose"]
    assert purpose["required"] is False and purpose["default"] == "before release"
    name = workflow["run-name"]
    assert "github.event_name == 'workflow_dispatch'" in name
    assert "github.ref_name" in name and "inputs.purpose" in name
    assert name.rstrip().endswith("|| ' ' }}")  # whitespace for every other event: GitHub's own title


def test_the_ci_log_names_the_slowest_tests():
    """R1 (0.4.7): the CI ran `pytest -q` alone, so its log said how long the run took but not where the
    time went. It now lists the 25 slowest test steps."""
    from pathlib import Path

    root = Path(reprintbuddy.__file__).parents[2]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "run: python -m pytest -q --durations=25" in workflow
