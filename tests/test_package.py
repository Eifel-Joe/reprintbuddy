import reprintbuddy


def test_version():
    assert reprintbuddy.__version__ == "0.3.11"


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


def test_python_3_12_is_the_floor_everywhere():
    """v0.3.9 p101: below 3.12 sqlite3 lacks `SQLITE_DBCONFIG_DEFENSIVE`, the setting that keeps a crafted
    backup from writing into its own schema. The package, the CI matrix and the code agree on 3.12."""
    import re
    import tomllib
    from pathlib import Path

    root = Path(reprintbuddy.__file__).parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["requires-python"] == ">=3.12"
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = re.search(r"python:\s*\[([^\]]*)\]", workflow).group(1)
    assert [v.strip().strip("\"'") for v in versions.split(",")] == ["3.12", "3.13"]
    assert "3.11" not in workflow


def test_no_code_is_left_that_only_serves_python_3_11():
    from pathlib import Path

    source = (Path(reprintbuddy.__file__).parent / "ops" / "fullbackup.py").read_text(encoding="utf-8")
    assert 'getattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"' not in source
    assert "on 3.11" not in source
