"""v0.4.9 p500: download, check and unpack a release package (Spec 11.3a "Paket je Release").

No network: the downloads get a fake opener; the archives are built in the tests.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import urllib.request
from pathlib import Path

import pytest

from reprintbuddy.ops import package as pk

TAG = "v1.0.0"
TOP = f"reprintbuddy-{TAG}"


def build_tar(path: Path, entries: list[tuple[str, bytes | None]], *, links: list[tuple[str, str]] = ()) -> Path:
    """A gzip tar with files (`bytes`) or folders (`None`) and symbolic links (name, target)."""
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                archive.addfile(info)
            else:
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
        for name, target in links:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return path


def good_tar(path: Path, tag: str = TAG) -> Path:
    top = f"reprintbuddy-{tag}"
    return build_tar(path, [(top, None), (f"{top}/pyproject.toml", b"[project]\n"), (f"{top}/install/install.sh", b"#!/bin/sh\n")])


# --- checksum ----------------------------------------------------------------------------------


def test_expected_sha256_reads_the_line_sha256sum_writes():
    text = "a" * 64 + "  reprintbuddy-v1.0.0.tar.gz\n"
    assert pk.expected_sha256(text, "reprintbuddy-v1.0.0.tar.gz") == "a" * 64


@pytest.mark.parametrize(
    "text",
    [
        "a" * 64 + "  other.tar.gz\n",
        "a" * 64 + "  reprintbuddy-v1.0.0.tar.gz\n" + "b" * 64 + "  reprintbuddy-v1.0.0.tar.gz\n",
        "a" * 63 + "  reprintbuddy-v1.0.0.tar.gz\n",
        "g" * 64 + "  reprintbuddy-v1.0.0.tar.gz\n",
        "",
    ],
)
def test_a_checksum_file_of_another_form_is_unusable(text):
    with pytest.raises(pk.PackageError) as raised:
        pk.expected_sha256(text, "reprintbuddy-v1.0.0.tar.gz")
    assert raised.value.reason == "package_unusable"


def test_verify_compares_the_file_and_githubs_own_checksum(tmp_path):
    path = tmp_path / "p"
    path.write_bytes(b"package")
    right = hashlib.sha256(b"package").hexdigest()
    assert pk.verify(path, right, None) == right
    assert pk.verify(path, right, "sha256:" + right) == right
    assert pk.verify(path, right.upper(), "SHA256:" + right.upper()) == right
    assert pk.verify(path, right, "md5:whatever") == right  # an unknown form is not GitHub's sha256: ignored
    for expected, digest in ((("0" * 64), None), (right, "sha256:" + "0" * 64)):
        with pytest.raises(pk.PackageError) as raised:
            pk.verify(path, expected, digest)
        assert raised.value.reason == "checksum_mismatch"


# --- unpack ------------------------------------------------------------------------------------


def test_unpack_puts_the_version_into_its_own_folder(tmp_path):
    releases = tmp_path / "releases"
    target = pk.unpack(good_tar(tmp_path / "p.tar.gz"), releases, TAG)
    assert target == releases / TAG
    assert (target / "pyproject.toml").read_text() == "[project]\n"
    assert (target / "install" / "install.sh").is_file()
    assert sorted(path.name for path in releases.iterdir()) == [TAG]


def test_unpack_replaces_a_half_folder_left_by_an_interrupted_run(tmp_path):
    releases = tmp_path / "releases"
    (releases / TAG).mkdir(parents=True)
    (releases / TAG / "stale.txt").write_text("x")
    target = pk.unpack(good_tar(tmp_path / "p.tar.gz"), releases, TAG)
    assert not (target / "stale.txt").exists()
    assert (target / "pyproject.toml").is_file()


@pytest.mark.parametrize(
    "entries, links",
    [
        ([(TOP, None), (f"{TOP}/pyproject.toml", b"x"), (f"{TOP}/../../evil", b"x")], []),
        ([(TOP, None), (f"{TOP}/pyproject.toml", b"x"), ("/tmp/evil", b"x")], []),
        ([(TOP, None), (f"{TOP}/pyproject.toml", b"x")], [(f"{TOP}/out", "/")]),
        ([(TOP, None), (f"{TOP}/pyproject.toml", b"x")], [(f"{TOP}/out", "../../..")]),
        ([("other", None), ("other/pyproject.toml", b"x")], []),
        ([(TOP, None), (f"{TOP}/pyproject.toml", b"x"), ("stray.txt", b"x")], []),
        ([(TOP, None), (f"{TOP}/README.md", b"x")], []),  # no pyproject.toml: nothing pip could install
    ],
)
def test_a_bad_archive_is_refused_and_leaves_nothing(tmp_path, entries, links):
    releases = tmp_path / "releases"
    archive = build_tar(tmp_path / "bad.tar.gz", entries, links=links)
    with pytest.raises(pk.PackageError) as raised:
        pk.unpack(archive, releases, TAG)
    assert raised.value.reason == "package_unusable"
    assert not (releases / TAG).exists()
    assert not (tmp_path / "evil").exists()
    assert list(releases.iterdir()) == [] if releases.exists() else True


def test_a_file_that_is_no_archive_is_unusable(tmp_path):
    path = tmp_path / "p.tar.gz"
    path.write_bytes(b"<html>not found</html>")
    with pytest.raises(pk.PackageError) as raised:
        pk.unpack(path, tmp_path / "releases", TAG)
    assert raised.value.reason == "package_unusable"


# --- download ----------------------------------------------------------------------------------


class FakeResponse(io.BytesIO):
    def __init__(self, data: bytes, status: int = 200):
        super().__init__(data)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    def __init__(self, response=None, error: Exception | None = None):
        self.response, self.error = response, error
        self.urls: list[str] = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url if hasattr(request, "full_url") else request)
        if self.error is not None:
            raise self.error
        return self.response


def test_download_writes_the_file(tmp_path):
    dest = tmp_path / "p.tar.gz"
    opener = FakeOpener(FakeResponse(b"data"))
    pk.urllib_download("https://github.com/o/r/releases/download/v1.0.0/p.tar.gz", dest, _opener=opener)
    assert dest.read_bytes() == b"data"
    assert opener.urls == ["https://github.com/o/r/releases/download/v1.0.0/p.tar.gz"]


def test_download_refuses_a_file_over_the_limit_and_leaves_none(tmp_path, monkeypatch):
    monkeypatch.setattr(pk, "MAX_PACKAGE_BYTES", 10)
    dest = tmp_path / "p.tar.gz"
    with pytest.raises(pk.PackageError) as raised:
        pk.urllib_download("https://github.com/x", dest, _opener=FakeOpener(FakeResponse(b"x" * 11)))
    assert raised.value.reason == "fetch_failed"
    assert not dest.exists()


@pytest.mark.parametrize("error, reason", [(OSError("unreachable"), "fetch_failed")])
def test_a_failed_download_leaves_no_file(tmp_path, error, reason):
    dest = tmp_path / "p.tar.gz"
    with pytest.raises(pk.PackageError) as raised:
        pk.urllib_download("https://github.com/x", dest, _opener=FakeOpener(error=error))
    assert raised.value.reason == reason
    assert not dest.exists()


def test_a_missing_package_is_named_as_such(tmp_path):
    import urllib.error

    missing = urllib.error.HTTPError("https://github.com/x", 404, "Not Found", {}, None)
    with pytest.raises(pk.PackageError) as raised:
        pk.urllib_download("https://github.com/x", tmp_path / "p", _opener=FakeOpener(error=missing))
    assert raised.value.reason == "package_missing"


def test_download_refuses_an_address_that_is_no_https(tmp_path):
    opener = FakeOpener(FakeResponse(b"data"))
    with pytest.raises(pk.PackageError):
        pk.urllib_download("http://github.com/x", tmp_path / "p", _opener=opener)
    assert opener.urls == []


def test_a_redirect_to_an_address_that_is_no_https_is_refused():
    handler = pk.HttpsOnlyRedirect()
    request = urllib.request.Request("https://github.com/x")
    with pytest.raises(pk.PackageError):
        handler.redirect_request(request, None, 302, "Found", {}, "http://objects.example/x")
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://objects.example/x") is not None


def test_the_get_of_the_helper_follows_no_redirect_and_keeps_the_size_limit(monkeypatch):
    import urllib.error

    moved = urllib.error.HTTPError("https://api.github.com/x", 302, "Found", {}, None)
    assert pk.urllib_get("https://api.github.com/x", {}, _opener=FakeOpener(error=moved)) == (302, b"")
    assert pk.urllib_get("https://api.github.com/x", {}, _opener=FakeOpener(FakeResponse(b"[]"))) == (200, b"[]")
    monkeypatch.setattr(pk, "MAX_RESPONSE_BYTES", 3)
    with pytest.raises(pk.PackageError):
        pk.urllib_get("https://api.github.com/x", {}, _opener=FakeOpener(FakeResponse(b"[1234]")))


# --- tidying up --------------------------------------------------------------------------------


def test_prune_releases_keeps_the_named_versions(tmp_path):
    for name in ("v0.9.0", "v1.0.0", "v1.0.1", ".download"):
        (tmp_path / "releases" / name).mkdir(parents=True)
    assert sorted(pk.prune_releases(tmp_path, keep={"v1.0.1", "v1.0.0"})) == [".download", "v0.9.0"]
    assert sorted(path.name for path in (tmp_path / "releases").iterdir()) == ["v1.0.0", "v1.0.1"]


def test_remove_git_copy_keeps_what_belongs_to_the_package_way(tmp_path):
    for name in (".venv", "releases/v1.0.0", ".git/objects", "src/reprintbuddy"):
        (tmp_path / name).mkdir(parents=True)
    for name in ("release.json", "README.md", "pyproject.toml"):
        (tmp_path / name).write_text("x")
    removed, failed = pk.remove_git_copy(tmp_path)
    assert sorted(removed) == [".git", "README.md", "pyproject.toml", "src"]
    assert failed == []
    assert sorted(path.name for path in tmp_path.iterdir()) == [".venv", "release.json", "releases"]


def test_review_an_archive_whose_compressed_stream_is_broken_is_unusable(tmp_path, monkeypatch):
    """Final review, minor 1: a gzip stream that breaks inside raised `zlib.error`, which escaped `unpack`
    (traceback, the status stayed "running"). Anything the archive library raises means "unusable"."""
    import zlib

    def broken(*args, **kwargs):
        raise zlib.error("Error -3 while decompressing data")

    archive = good_tar(tmp_path / "p.tar.gz")
    monkeypatch.setattr(pk.tarfile, "open", broken)
    with pytest.raises(pk.PackageError) as raised:
        pk.unpack(archive, tmp_path / "releases", TAG)
    assert raised.value.reason == "package_unusable"
    assert list((tmp_path / "releases").iterdir()) == []


def test_review_remove_git_copy_never_removes_the_folder_of_the_database(tmp_path):
    """Final review, minor 7: "Daten nie" (Spec 11.3a), also for `reprintbuddy update --db` pointing into
    the program folder by hand."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "reprintbuddy.db"
    db.write_bytes(b"x")
    (tmp_path / "loose.db").write_bytes(b"y")
    removed, failed = pk.remove_git_copy(tmp_path, protect=[db, tmp_path / "loose.db"])
    assert removed == [".git"]
    assert sorted(failed) == ["data", "loose.db"]
    assert db.exists() and (tmp_path / "loose.db").exists()
