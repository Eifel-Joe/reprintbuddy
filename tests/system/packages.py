"""The two packages the end-to-end run offers through its stand-in for GitHub (tests/system/README.md).

- The last stable version S: the newest tag vX.Y.Z whose database steps lie on the line of the state under test
  (`tools/stable_head.py`; a stable tag sits on a merge commit in `main`, not in the history of `dev`). Its package
  is built from the tag as the workflow "Release package" builds it: `git archive --prefix=reprintbuddy-<tag>/`,
  packed without a time stamp, beside it the line `sha256sum` writes.
- The state under test P as the pre-release after S, vX.Y.(Z+1)-beta.1: a way back exists only from a pre-release
  (Spec 11.3a). The update helper checks that the installed code says the version of the tag, and the installer
  installs the tag in its `RELEASE_TAG`: these two lines, and only these, are set to the tag in the archive.

Runs on the CI runner (git and this code at hand), not in the container: `python tests/system/packages.py --out DIR`
writes `releases.json` (newest first, as GitHub lists them), `<tag>/` with package and checksum, the install script
of each version and `plan.json`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from check_release_notes import rework_steps  # noqa: E402 - the release tools, found through the line above
from stable_head import last_stable_tag  # noqa: E402

from reprintbuddy.ops.migrationsteps import StepsError  # noqa: E402
from reprintbuddy.ops.releases import REWORK_LINE  # noqa: E402
from reprintbuddy.ops.versions import RELEASE_TAG, VERSION_TAG  # noqa: E402

INIT = "src/reprintbuddy/__init__.py"
INSTALL = "install/install.sh"
_VERSION_LINE = re.compile(rb'^__version__ = "[^"\n]*"$', re.MULTILINE)
_RELEASE_TAG_LINE = re.compile(rb'^RELEASE_TAG="[^"\n]*"$', re.MULTILINE)


class PackageBuildError(Exception):
    """The packages cannot be built; the message says why."""


def _git(repo: Path, *args: str) -> bytes:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if done.returncode != 0:
        raise PackageBuildError(f"git {' '.join(args)}: {done.stderr.decode(errors='replace').strip()}")
    return done.stdout


def last_stable(repo: Path) -> str:
    tag = last_stable_tag(repo, "HEAD")
    if tag is None:
        raise PackageBuildError("no stable version on the line of HEAD (fetch the tags: fetch-depth 0)")
    return tag


def prerelease_after(stable: str) -> str:
    match = RELEASE_TAG.fullmatch(stable)
    if match is None:
        raise PackageBuildError(f"{stable} is not a stable version tag")
    major, minor, patch = (int(part) for part in match.groups())
    return f"v{major}.{minor}.{patch + 1}-beta.1"


def _replace_once(data: bytes, pattern: re.Pattern[bytes], line: bytes, what: str) -> bytes:
    new, count = pattern.subn(line, data)
    if count != 1:
        raise PackageBuildError(f"{what} is there {count} times, not once")
    return new


def archive(repo: Path, ref: str, tag: str, *, set_version: bool = False, check_version: bool = True) -> bytes:
    """The package of `ref` as the release `tag`. Without `set_version` the code must already say that version
    (`check_version=False` only for the test that compares with the archive as git writes it)."""
    if VERSION_TAG.fullmatch(tag) is None:
        raise PackageBuildError(f"{tag} is not a version tag")
    top = f"reprintbuddy-{tag}"
    # as on Linux, whatever the git of this machine converts on checkout (core.autocrlf on Windows)
    raw = _git(repo, "-c", "core.autocrlf=false", "-c", "core.eol=lf", "archive", "--format=tar", f"--prefix={top}/", ref)
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as source:
        members = source.getmembers()  # reads the global header too: git archive puts the commit id there
        target = tarfile.open(fileobj=out, mode="w:", format=tarfile.PAX_FORMAT, pax_headers=dict(source.pax_headers))
        for item in members:
            data = source.extractfile(item).read() if item.isfile() else None  # type: ignore[union-attr]
            if item.name == f"{top}/{INIT}" and data is not None:
                if set_version:
                    data = _replace_once(data, _VERSION_LINE, f'__version__ = "{tag[1:]}"'.encode(), "__version__")
                elif check_version and f'__version__ = "{tag[1:]}"'.encode() not in data:
                    raise PackageBuildError(f"the code of {ref} does not say version {tag[1:]}")
            if item.name == f"{top}/{INSTALL}" and data is not None and set_version:
                data = _replace_once(data, _RELEASE_TAG_LINE, f'RELEASE_TAG="{tag}"'.encode(), "RELEASE_TAG")
            if data is None:
                target.addfile(item)
            else:
                item.size = len(data)
                target.addfile(item, io.BytesIO(data))
        target.close()
    # gzip without file name and time stamp, as `gzip -n -9` in the release workflow: the same bytes every time
    return gzip.compress(out.getvalue(), compresslevel=9, mtime=0)


def checksum_line(name: str, data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


def rework_ahead(repo: Path) -> bool:
    """Do the database steps after the stable head rework data (the notes must then begin with the rework line)?"""
    try:
        return bool(rework_steps(repo, "HEAD"))
    except StepsError as error:
        raise PackageBuildError(str(error)) from None


def notes(tag: str, *, rework: bool) -> str:
    text = f"End-to-end test release {tag}.\n"
    return f"{REWORK_LINE}\n\n{text}" if rework else text


def _member(data: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as package:
        found = package.extractfile(name)
        if found is None:
            raise PackageBuildError(f"{name} is missing in the package")
        return found.read()


def build(repo: Path, out: Path) -> dict[str, object]:
    stable = last_stable(repo)
    pre = prerelease_after(stable)
    rework = rework_ahead(repo)
    out.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).replace(microsecond=0)
    listed = []
    for tag, ref, is_pre, age in ((pre, "HEAD", True, 0), (stable, f"refs/tags/{stable}", False, 1)):
        data = archive(repo, ref, tag, set_version=is_pre)
        name = f"reprintbuddy-{tag}.tar.gz"
        folder = out / tag
        folder.mkdir(exist_ok=True)
        (folder / name).write_bytes(data)
        (folder / f"{name}.sha256").write_text(checksum_line(name, data), encoding="utf-8", newline="\n")
        (out / f"install-{tag}.sh").write_bytes(_member(data, f"reprintbuddy-{tag}/{INSTALL}"))
        listed.append(
            {
                "tag": tag,
                "prerelease": is_pre,
                "published_at": (now - timedelta(days=age)).isoformat().replace("+00:00", "Z"),
                "body": notes(tag, rework=rework) if is_pre else f"Stable release {tag}.\n",
            }
        )
    (out / "releases.json").write_text(json.dumps(listed, indent=2) + "\n", encoding="utf-8", newline="\n")
    plan: dict[str, object] = {"stable": stable, "prerelease": pre, "rework": rework}
    (out / "plan.json").write_text(json.dumps(plan) + "\n", encoding="utf-8", newline="\n")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = build(args.repo, args.out)
    except PackageBuildError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    print(json.dumps(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
