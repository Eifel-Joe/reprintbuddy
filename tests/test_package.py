import reprintbuddy


def test_version():
    assert reprintbuddy.__version__ == "0.3.8"


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
