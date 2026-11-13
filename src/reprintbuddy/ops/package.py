"""Download, check and unpack the package of a release (v0.4.9 p500, Spec 11.3a "Paket je Release").

The update helper runs this while pip replaces the installed packages, so only the standard library is
used and everything is imported at the top (see `ops/updater.py`). The web process never downloads.

- Download only over HTTPS, after every redirect too (GitHub serves the file from its own download host),
  at most `MAX_PACKAGE_BYTES`, at most `DOWNLOAD_TIMEOUT` seconds in all, no proxy from the environment.
- The checksum file is the line `sha256sum` writes (`<64 hex>  <name>`). GitHub's own `digest` of the
  asset is compared too when it has the form `sha256:<64 hex>`; its form is not documented, any other
  form is left aside rather than guessed.
- The archive must hold exactly one top folder `reprintbuddy-<tag>/` with a `pyproject.toml`; it is
  unpacked with tarfile's "data" filter (no absolute paths, nothing outside, no links leaving the
  folder) into a temporary folder first and then moved to `releases/<tag>`. A refused archive leaves
  nothing behind.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from reprintbuddy.ops.installation import MARKER, RELEASES_DIR
from reprintbuddy.ops.releasenotes import MAX_RESPONSE_BYTES, TIMEOUT

MAX_PACKAGE_BYTES = 50_000_000
DOWNLOAD_TIMEOUT = 300.0  # seconds for one download in all
_STEP_TIMEOUT = 30.0  # seconds for one step of the connection (connect, one read)
_CHUNK = 64 * 1024
_CHECKSUM_LINE = re.compile(r"^([0-9a-fA-F]{64}) [ *](.+)$")
_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$", re.IGNORECASE)
KEEP_IN_INSTALL_DIR = frozenset({".venv", RELEASES_DIR, MARKER})

Reason = Literal["package_missing", "checksum_mismatch", "package_unusable", "fetch_failed"]

# One download: the file at `url` into `dest`. Raises `PackageError`. Handed in, so tests never download.
Download = Callable[[str, Path], None]


class PackageError(Exception):
    """The package could not be had or not be used; `reason` is the code the update card shows."""

    def __init__(self, reason: Reason, detail: str = ""):
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail


class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another HTTPS address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib's signature
        if not str(newurl).lower().startswith("https://"):
            raise PackageError("fetch_failed", "a redirect to an address without HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # urllib then raises HTTPError with the 3xx status


def _download_opener() -> Any:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), HttpsOnlyRedirect())


def _api_opener() -> Any:
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def urllib_download(url: str, dest: Path, *, _opener: Any = None) -> None:
    """The production download. `_opener` is for the tests only."""
    if not url.lower().startswith("https://"):
        raise PackageError("fetch_failed", "an address without HTTPS")
    opener = _opener or _download_opener()
    deadline = time.monotonic() + DOWNLOAD_TIMEOUT
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
        with opener.open(request, timeout=_STEP_TIMEOUT) as response, open(dest, "wb") as file:
            size = 0
            # p550 (v0.4.10): `read1` returns what has arrived (one receive, at most `_STEP_TIMEOUT`), so
            # the deadline is looked at after every piece. Root: `read(_CHUNK)` waited for a whole block,
            # and a server sending a byte now and then held the update until systemd's 30 minutes.
            # Rejected: a timer thread that closes the socket (more moving parts for the same bound).
            # Tests: test_release_package.py "p550".
            while chunk := response.read1(_CHUNK):
                size += len(chunk)
                if size > MAX_PACKAGE_BYTES:
                    raise PackageError("fetch_failed", "the package is larger than allowed")
                if time.monotonic() > deadline:
                    raise PackageError("fetch_failed", "the download took too long")
                file.write(chunk)
    except PackageError:
        dest.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as error:
        dest.unlink(missing_ok=True)
        if error.code == 404:
            raise PackageError("package_missing", "HTTP 404") from None
        raise PackageError("fetch_failed", f"HTTP {error.code}") from None
    except Exception as error:  # noqa: BLE001 - the network, a timeout, a full disk
        dest.unlink(missing_ok=True)
        raise PackageError("fetch_failed", type(error).__name__) from None


def urllib_get(url: str, headers: Mapping[str, str], *, _opener: Any = None) -> tuple[int, bytes]:
    """The one request to the release list from the update helper (no httpx there): no redirect followed,
    at most `MAX_RESPONSE_BYTES`, at most `TIMEOUT` seconds in all (p550, v0.4.10: looked at after every
    piece that arrives, as for the download). `_opener` is for the tests only."""
    opener = _opener or _api_opener()
    deadline = time.monotonic() + TIMEOUT
    try:
        with opener.open(urllib.request.Request(url, headers=dict(headers)), timeout=TIMEOUT) as response:
            data = bytearray()
            while chunk := response.read1(_CHUNK):
                data += chunk
                if len(data) > MAX_RESPONSE_BYTES:
                    raise PackageError("fetch_failed", "answer too large")
                if time.monotonic() > deadline:
                    raise PackageError("fetch_failed", "timeout")
            return response.status, bytes(data)
    except urllib.error.HTTPError as error:
        return error.code, b""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def expected_sha256(checksum_text: str, name: str) -> str:
    """The checksum of `name` from a checksum file of exactly one line in the form of `sha256sum`."""
    lines = [line for line in checksum_text.splitlines() if line.strip()]
    found = _CHECKSUM_LINE.match(lines[0].strip()) if len(lines) == 1 else None
    if found is None or found[2].strip() != name:
        raise PackageError("package_unusable", "the checksum file has another form")
    return found[1].lower()


def verify(path: Path, expected: str, digest: str | None) -> str:
    """The checksum of `path`, when it agrees with the checksum file and with GitHub's own (if given)."""
    actual = sha256_of(path)
    if actual != expected.lower():
        raise PackageError("checksum_mismatch", "the package does not match its checksum file")
    github = _DIGEST.match(digest.strip()) if digest else None
    if github is not None and github[1].lower() != actual:
        raise PackageError("checksum_mismatch", "the package does not match GitHub's checksum")
    return actual


def unpack(archive: Path, dest: Path, tag: str) -> Path:
    """Unpack `archive` into `dest/<tag>` (a folder there is replaced). Raises `PackageError`."""
    top = f"reprintbuddy-{tag}"
    dest.mkdir(parents=True, exist_ok=True)
    work = dest / f".unpack-{tag}"
    target = dest / tag
    shutil.rmtree(work, ignore_errors=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                parts = PurePosixPath(member.name).parts
                if not parts or parts[0] != top or member.name.startswith("/") or ".." in parts:
                    raise PackageError("package_unusable", "the archive holds more than the version's folder")
                # p552 (v0.4.10): the entry of the folder itself must be a folder. Root: a link `<top>` -> `.`
                # passed the names, its files landed beside it and `releases/<tag>` became a link (nothing
                # outside, but the install then failed into a rollback). Tests: test_release_package.py "p552".
                if len(parts) == 1 and not member.isdir():
                    raise PackageError("package_unusable", "the version's folder is no real folder")
            work.mkdir()
            tar.extractall(work, members=members, filter="data")
        unpacked = work / top
        if not (unpacked / "pyproject.toml").is_file():
            raise PackageError("package_unusable", "the archive has no pyproject.toml")
        shutil.rmtree(target, ignore_errors=True)
        unpacked.rename(target)
    except PackageError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    except Exception as error:  # noqa: BLE001 - tarfile, gzip, zlib (review: zlib.error escaped): unusable
        shutil.rmtree(work, ignore_errors=True)
        raise PackageError("package_unusable", type(error).__name__) from None
    shutil.rmtree(work, ignore_errors=True)
    return target


def prune_releases(install_dir: Path, keep: set[str]) -> list[str]:
    """Remove every entry of `releases/` but the versions in `keep`; the names removed."""
    folder = install_dir / RELEASES_DIR
    removed: list[str] = []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry.name in keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        if not entry.exists():
            removed.append(entry.name)
    return removed


def remove_git_copy(install_dir: Path, *, protect: Iterable[Path] = ()) -> tuple[list[str], list[str]]:
    """Remove the old git copy: everything in the program folder but `.venv`, `releases` and
    `release.json`. An entry that is or holds a path of `protect` (the database: "Daten nie", Spec 11.3a)
    stays and counts as not removed. Never raises; returns (removed, could not be removed)."""
    removed: list[str] = []
    failed: list[str] = []
    try:
        entries = list(install_dir.iterdir())
        kept = [path.resolve() for path in protect]
    except OSError:
        return removed, failed
    for entry in entries:
        if entry.name in KEEP_IN_INSTALL_DIR:
            continue
        here = entry.resolve() if not entry.is_symlink() else entry.absolute()
        if any(path == here or path.is_relative_to(here) for path in kept):
            failed.append(entry.name)
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(entry.name)
        except OSError:
            failed.append(entry.name)
    return removed, failed
