"""The package of a release as the release workflow builds it, for tests of the update and the way back."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping
from pathlib import Path

VERSIONS_DIR = Path(__file__).resolve().parents[1] / "src" / "reprintbuddy" / "migrations" / "versions"


def build_archive(path: Path, tag: str, versions: Mapping[str, str] | None = None) -> Path:
    """The package of `tag` as the release workflow builds it: one folder `reprintbuddy-<tag>/`.

    It carries migration steps (the way back reads the head from them, Spec 11.3a "Rückweg"): those of
    this code unless `versions` (file name -> text) names others."""
    top = f"reprintbuddy-{tag}"
    if versions is None:
        versions = {item.name: item.read_text(encoding="utf-8") for item in VERSIONS_DIR.glob("*.py")}
    files = [(f"{top}/pyproject.toml", b"[project]\n"), (f"{top}/install/install.sh", b"#!/bin/sh\n")]
    files += [(f"{top}/src/reprintbuddy/migrations/versions/{name}", text.encode()) for name, text in versions.items()]
    with tarfile.open(path, "w:gz") as archive:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path
