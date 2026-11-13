"""A fake GitHub release list for the tests of the check and the update (v0.4.9 p500).

GitHub is never asked for real: `FakeGitHub` stands in for `OpsContext.http_get` (and the update helper's
`http_get`), answers every request with one scripted reply and counts the requests.
"""

from __future__ import annotations

import dataclasses
import json

from reprintbuddy.ops.releases import checksum_name, package_name

ORIGIN = "https://github.com/owner/repo.git"
PAGE_URL = "https://github.com/owner/repo/releases"
DOWNLOAD = "https://github.com/owner/repo/releases/download"


def release_item(
    tag: str,
    *,
    body: object = "Notes.",
    draft: bool = False,
    prerelease: bool | None = None,
    published: str | None = "2026-11-09T15:19:00Z",
    package: bool = True,
    checksum: bool = True,
    digest: str | None = None,
) -> dict:
    """One release of GitHub's answer, with package and checksum file unless told otherwise."""
    assets = []
    if package:
        item = {"name": package_name(tag), "state": "uploaded", "browser_download_url": f"{DOWNLOAD}/{tag}/{package_name(tag)}"}
        if digest is not None:
            item["digest"] = digest
        assets.append(item)
    if checksum:
        assets.append({"name": checksum_name(tag), "state": "uploaded", "browser_download_url": f"{DOWNLOAD}/{tag}/{checksum_name(tag)}"})
    return {
        "tag_name": tag,
        "body": body,
        "draft": draft,
        "prerelease": ("-beta." in tag) if prerelease is None else prerelease,
        "published_at": published,
        "assets": assets,
    }


def release_list(*entries: str | dict) -> list[dict]:
    """GitHub's answer, newest first as GitHub sends it; a tag stands for a complete release."""
    return [release_item(entry) if isinstance(entry, str) else entry for entry in entries]


class FakeGitHub:
    def __init__(self, releases: list[dict] | tuple = (), *, status: int = 200, error: Exception | None = None):
        self.status, self.error = status, error
        self.body = json.dumps(release_list(*releases)).encode()
        self.calls: list[str] = []

    def __call__(self, url, headers):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.status, self.body


def use_github(env, github: FakeGitHub) -> FakeGitHub:
    """Let the app of `env` (an `ops_env`) ask `github` instead of GitHub."""
    app = env.client.app
    app.state.ops = dataclasses.replace(app.state.ops, http_get=github)
    return github


def github_of(env) -> FakeGitHub:
    return env.client.app.state.ops.http_get


def offline_github(url, headers):
    """The default of the tests: no GitHub at all (a check then fails, nothing leaves the machine)."""
    return 503, b""
