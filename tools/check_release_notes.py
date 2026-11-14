"""Do the notes of a release agree with the rework marks of its database steps? (Spec 11.3a "Umbau nur angekündigt")

A pre-release whose steps after the last stable version include one marked `rework = True` must begin its
notes with the fixed rework line (`reprintbuddy.ops.releases.REWORK_LINE`); a pre-release without such a
step must not, and a stable release never does. A step after the stable head without a `rework` mark counts
as rework, as on the way back (`ops/stablereturn.py`).

The release workflow runs this before it attaches the package: a release that does not agree gets no package,
and no Reprintbuddy offers a release without one. After the notes are corrected, the workflow is started again
by hand. The main session runs it before each release too:

    python tools/check_release_notes.py --tag v1.1.0-beta.1 --notes-file notes.md

Exit 0 when everything agrees, 1 with the reason on stderr otherwise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stable_head import sources_at, stable_head  # noqa: E402 - the sibling tool, found through the line above

from reprintbuddy.ops.migrationsteps import (  # noqa: E402
    REWORK_ATTR,
    StepsError,
    literals,
    parse_graph,
    single_head,
)
from reprintbuddy.ops.releases import REWORK_LINE, starts_with_rework  # noqa: E402
from reprintbuddy.ops.versions import is_prerelease_tag, parse_version  # noqa: E402


def rework_steps(repo: Path, tag: str) -> list[str]:
    """The steps after the last stable head up to the head of `tag` that are (or count as) rework."""
    base = stable_head(repo, tag)
    if base is None:
        raise StepsError(f"no stable version before {tag} in the history")
    sources = sources_at(repo, tag)
    if sources is None:
        raise StepsError(f"the migration steps of {tag} cannot be read")
    graph = parse_graph(sources)
    marks = {}
    for source in sources:
        values = literals(source)
        if isinstance(values.get("revision"), str):
            marks[values["revision"]] = values.get(REWORK_ATTR)
    found = []
    current: str | None = single_head(graph)
    while current != base:
        if current is None:
            raise StepsError(f"the stable head {base} is not in the history of {tag}")
        if marks.get(current) is not False:  # marked as rework, or not marked at all
            found.append(current)
        current = graph[current]
    return list(reversed(found))


def check(repo: Path, tag: str, body: str) -> str | None:
    """None when the notes agree with the steps, else the reason (English, for the workflow log)."""
    if parse_version(tag) is None:
        return f"{tag} is not a version tag"
    has_line = starts_with_rework(body)
    if not is_prerelease_tag(tag):
        return f"the notes of the stable release {tag} begin with the rework line; remove it" if has_line else None
    try:
        rework = rework_steps(repo, tag)
    except StepsError as error:
        return str(error)
    if rework and not has_line:
        return (
            f"step(s) {', '.join(rework)} of {tag} rework existing data; the notes must begin with the line:\n"
            f"{REWORK_LINE}"
        )
    if has_line and not rework:
        return f"the notes of {tag} begin with the rework line, but no step after the last stable version reworks data"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--notes-file", required=True, type=Path)
    parser.add_argument("--repo", default=".", type=Path)
    args = parser.parse_args(argv)
    said = check(args.repo, args.tag, args.notes_file.read_text(encoding="utf-8"))
    if said is not None:
        print(f"::error::{said}" if "\n" not in said else said, file=sys.stderr)
        return 1
    print(f"the notes of {args.tag} agree with its database steps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
