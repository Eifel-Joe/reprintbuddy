"""Templates held in memory from the start of the process.

An update replaces the installed files while the old process still answers (the restart comes
at the end). A loader that reads a template from disk whenever it is needed would hand the old
code the new templates, which ask for things the old code does not have. This loader reads all
templates once and serves them from memory, so a process renders what it started with.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from jinja2 import BaseLoader, Environment, TemplateNotFound


class PreloadedLoader(BaseLoader):
    def __init__(self, directory: Path) -> None:
        self._sources: dict[str, tuple[str, str]] = {
            path.relative_to(directory).as_posix(): (path.read_text(encoding="utf-8"), str(path))
            for path in sorted(directory.rglob("*.html"))
        }

    def get_source(self, environment: Environment, template: str) -> tuple[str, str, Callable[[], bool]]:
        try:
            source, filename = self._sources[template]
        except KeyError:
            raise TemplateNotFound(template) from None
        return source, filename, lambda: True  # never changes: the file on disk is not looked at again

    def list_templates(self) -> list[str]:
        return sorted(self._sources)
