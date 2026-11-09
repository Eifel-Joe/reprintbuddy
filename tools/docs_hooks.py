"""MkDocs hook: puts the version of this release into the pages.

A page writes `{{ version }}` where the version belongs (the tag in the installation
command, for example). The version is read from `src/reprintbuddy/__init__.py` without
importing the package, so building the pages needs nothing but MkDocs.
"""

import re
from pathlib import Path

INIT = Path(__file__).resolve().parents[1] / "src" / "reprintbuddy" / "__init__.py"


def read_version() -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', INIT.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise RuntimeError(f"no __version__ in {INIT}")
    return match.group(1)


def on_page_markdown(markdown, page, config, files):
    return markdown.replace("{{ version }}", read_version())
