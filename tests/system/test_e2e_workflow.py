"""The end-to-end job (.github/workflows/e2e.yml): its own workflow beside the CI, without secrets, runs the run.

The required checks of the branch protection ("Tests (Python 3.13)", "Tests (Python 3.14)", "Install script",
"Documentation") are jobs of ci.yml; this job must not take or change one of their names."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
E2E = WORKFLOWS / "e2e.yml"
SYSTEM = ROOT / "tests" / "system"
REQUIRED = {"Tests (Python 3.13)", "Tests (Python 3.14)", "Install script", "Documentation"}


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads the key `on` as true
    return data


def test_the_job_has_its_own_name_and_the_required_checks_stay_in_the_ci() -> None:
    e2e = _load(E2E)
    assert e2e["name"] == "End-to-end"
    assert [job["name"] for job in e2e["jobs"].values()] == ["End-to-end (Debian 13)"]
    ci = _load(WORKFLOWS / "ci.yml")
    names = {job["name"].replace("${{ matrix.python }}", version) for job in ci["jobs"].values() for version in ("3.13", "3.14")}
    assert REQUIRED <= names


def test_it_runs_for_every_pull_request_and_by_hand() -> None:
    triggers = _load(E2E)["on"]
    assert "pull_request" in triggers
    # release/**: the branches a round is collected on run it on their push, before the pull request; a
    # workflow_dispatch reaches only workflows that are on the default branch already
    assert triggers["push"]["branches"] == ["main", "dev", "release/**"]
    assert "workflow_dispatch" in triggers


def test_it_needs_no_secret_and_only_reads_the_repository() -> None:
    text = E2E.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert _load(E2E)["permissions"] == {"contents": "read"}
    (job,) = _load(E2E)["jobs"].values()
    assert "permissions" not in job


def test_it_checks_out_the_tags_and_runs_the_run_and_keeps_the_logs_of_a_failure() -> None:
    (job,) = _load(E2E)["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    assert 0 < job["timeout-minutes"] <= 60
    steps = job["steps"]
    checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0  # the last stable version is found among the tags
    assert checkout["with"]["persist-credentials"] is False
    runs = [step.get("run", "") for step in steps]
    assert any("tests/system/run.sh" in run for run in runs)
    upload = next(step for step in steps if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
    assert upload["if"] == "failure()"


def test_the_container_has_systemd_and_the_run_script_starts_it_with_the_hosts_of_the_stand_in() -> None:
    containerfile = (SYSTEM / "Containerfile").read_text(encoding="utf-8")
    assert containerfile.splitlines()[0] == "FROM debian:13"
    for package in ("systemd", "systemd-sysv", "openssl", "python3"):
        assert f" {package} " in containerfile or f" {package}\n" in containerfile
    assert 'CMD ["/sbin/init"]' in containerfile
    run = (SYSTEM / "run.sh").read_text(encoding="utf-8")
    assert run.startswith("#!/usr/bin/env bash\n")
    assert "set -Eeuo pipefail" in run
    assert "--add-host github.com:127.0.0.1" in run and "--add-host api.github.com:127.0.0.1" in run
    assert "tests/system/packages.py" in run and "scenario.py" in run


def test_contributors_learn_what_the_job_does_and_that_docker_is_only_its_bench() -> None:
    for name, words in (
        ("CONTRIBUTING.md", ("End-to-end (Debian 13)", "tests/system/run.sh", "not a dependency")),
        ("CONTRIBUTING.de.md", ("End-to-end (Debian 13)", "tests/system/run.sh", "keine Abhängigkeit")),
    ):
        text = " ".join((ROOT / name).read_text(encoding="utf-8").split())
        for word in words:
            assert word in text, (name, word)
