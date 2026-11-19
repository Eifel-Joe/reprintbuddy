"""The stand-in for GitHub of the end-to-end run (tests/system/fakegithub.py), read by the production code: the
release list as the update helper and the pages read it, the package as both download it."""

from __future__ import annotations

import json
import socket
import ssl
import urllib.request
from pathlib import Path

import httpx
import pytest
import trustme

import fakegithub
import packages
from reprintbuddy.ops import package as pkg
from reprintbuddy.ops.releasenotes import _get, api_headers
from reprintbuddy.ops.releases import fetch_releases, newest_offered, parse_releases, return_target

REPO = ("e2e-owner", "reprintbuddy")
STABLE, PRE = "v1.2.0", "v1.2.1-beta.1"


def _release_dir(root: Path) -> Path:
    listed = []
    for tag, pre, body in ((PRE, True, "pre\n"), (STABLE, False, "stable\n")):
        name = f"reprintbuddy-{tag}.tar.gz"
        data = f"package {tag}".encode()
        (root / tag).mkdir(parents=True)
        (root / tag / name).write_bytes(data)
        (root / tag / f"{name}.sha256").write_text(packages.checksum_line(name, data), encoding="utf-8")
        listed.append({"tag": tag, "prerelease": pre, "published_at": "2026-11-19T10:00:00Z", "body": body})
    (root / "releases.json").write_text(json.dumps(listed), encoding="utf-8")
    return root


@pytest.fixture
def ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def server(tmp_path: Path, ca: trustme.CA, monkeypatch: pytest.MonkeyPatch):
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert(*fakegithub.HOSTS).configure_cert(context)
    log = tmp_path / "requests.log"
    fake = fakegithub.FakeGitHub(_release_dir(tmp_path / "releases"), "/".join(REPO), context=context, log=log)
    fake.start(port=0)
    real = socket.getaddrinfo

    def resolve(host, port, *args, **kwargs):  # what /etc/hosts does in the container
        if host in fakegithub.HOSTS and port in (443, "443", "https"):
            return real("127.0.0.1", fake.port, *args, **kwargs)
        return real(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    yield fake
    fake.stop()


def _opener(ca: trustme.CA, *handlers):
    context = ssl.create_default_context()
    ca.configure_trust(context)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context), *handlers)


def test_the_update_helper_reads_both_releases_with_package_and_checksum(server, ca) -> None:
    opener = _opener(ca, pkg._NoRedirect())
    releases = fetch_releases(REPO, lambda url, headers: pkg.urllib_get(url, headers, _opener=opener))
    assert [(item.tag, item.prerelease) for item in releases] == [(PRE, True), (STABLE, False)]
    assert newest_offered(releases, "prerelease").tag == PRE  # type: ignore[union-attr]
    assert newest_offered(releases, "stable").tag == STABLE  # type: ignore[union-attr]
    assert return_target(releases, PRE).tag == STABLE  # type: ignore[union-attr]
    assert releases[0].body == "pre\n"
    assert all(item.digest and item.digest.startswith("sha256:") for item in releases)
    assert server.unexpected == []


def test_the_pages_read_the_same_list_through_httpx(server, ca) -> None:
    context = ssl.create_default_context()
    ca.configure_trust(context)
    status, body = _get(
        f"https://api.github.com/repos/{REPO[0]}/{REPO[1]}/releases?per_page=30",
        api_headers(),
        httpx.HTTPTransport(verify=context),
    )
    assert status == 200
    assert [item.tag for item in parse_releases(json.loads(body))] == [PRE, STABLE]


def test_the_package_downloads_and_agrees_with_its_checksum_and_digest(server, ca, tmp_path: Path) -> None:
    opener = _opener(ca, pkg._NoRedirect())
    releases = fetch_releases(REPO, lambda url, headers: pkg.urllib_get(url, headers, _opener=opener))
    chosen = releases[0]
    download = _opener(ca, pkg.HttpsOnlyRedirect())
    package, checksum = tmp_path / "p.tar.gz", tmp_path / "p.sha256"
    pkg.urllib_download(chosen.package_url, package, _opener=download)
    pkg.urllib_download(chosen.checksum_url, checksum, _opener=download)
    expected = pkg.expected_sha256(checksum.read_text(encoding="utf-8"), f"reprintbuddy-{PRE}.tar.gz")
    assert pkg.verify(package, expected, chosen.digest) == expected
    assert package.read_bytes() == f"package {PRE}".encode()


def test_any_other_request_is_refused_and_recorded(server, ca, tmp_path: Path) -> None:
    download = _opener(ca, pkg.HttpsOnlyRedirect())
    with pytest.raises(pkg.PackageError) as raised:
        pkg.urllib_download("https://github.com/other/repo/releases/download/v1.2.0/x.tar.gz", tmp_path / "x", _opener=download)
    assert raised.value.reason == "package_missing"
    with pytest.raises(pkg.PackageError):
        pkg.urllib_download(f"https://github.com/{REPO[0]}/{REPO[1]}/releases/download/{PRE}/../../x", tmp_path / "y", _opener=download)
    assert len(server.unexpected) == 2
    assert server.log.read_text(encoding="utf-8").count("UNEXPECTED") == 2


def test_the_command_line_serves_until_stopped_and_writes_a_log(tmp_path: Path, ca: trustme.CA) -> None:
    cert = ca.issue_cert(*fakegithub.HOSTS)
    cert.private_key_and_cert_chain_pem.write_to_path(str(tmp_path / "server.pem"))
    args = fakegithub.parse_args(
        ["--root", str(tmp_path), "--repo", "o/n", "--cert", str(tmp_path / "server.pem"), "--log", str(tmp_path / "l")]
    )
    assert (args.port, args.repo, args.bind) == (443, "o/n", "127.0.0.1")
