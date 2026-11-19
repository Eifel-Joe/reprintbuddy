# End-to-end run

The job **End-to-end (Debian 13)** (`.github/workflows/e2e.yml`) plays the test plans of a pre-release on a
fresh Debian 13 with systemd, the way an installation goes through them:

1. the installer of the last stable version S installs it;
2. account and Bambuddy are set up on the pages, a part is added, a backup downloaded;
3. with "Include pre-releases" the state under test P is offered and installed by the update;
4. the installer of P runs again (the repair run of an existing installation);
5. the backup is restored through the page and through `reprintbuddy restore-backup` (on P);
6. the way back to S is started and stopped from outside (`systemctl stop reprintbuddy-update`) while pip
   installs, then while the service stops: P runs again each time, with its data;
7. the way back runs to the end: S runs, with its data.

After each step it checks the version (`release.json`, the program, the page), the parts, the card of the
update and the log, using the English texts of the installed version.

## How it is built

| File | Where it runs | What it does |
|---|---|---|
| `packages.py` | runner | S from its tag and P from the checkout, as the workflow "Release package" builds them; P is named `vX.Y.(Z+1)-beta.1` after S, with `__version__` and `RELEASE_TAG` set to that name and nothing else changed |
| `Containerfile` | runner | Debian 13 with systemd |
| `run.sh` | runner | builds, starts the container, runs `scenario.py` inside, collects the logs |
| `scenario.py` | container, as root | the steps above |
| `fakegithub.py` | container | GitHub's release list and downloads for the packages of `packages.py` |
| `fakebambuddy.py` | container | a read-only Bambuddy with no printers |
| `webclient.py`, `websteps.py` | container | fill in the forms the pages show, as a person would |

Nothing in Reprintbuddy knows about the stand-ins. `github.com` and `api.github.com` point to 127.0.0.1 in the
container (`--add-host`), and a certificate authority made for the run is trusted there: in the system's list (the
installer and the update helper) and in the certifi list of the installed environment (the pages ask GitHub with
httpx, which reads only that list). The stand-ins refuse and record any request they do not expect, and the run
fails on it. apt and PyPI are reached as on any installation.

## Running it yourself

On Linux with Docker, from the repository with its tags:

```bash
python -m pip install .
bash tests/system/run.sh /tmp/e2e
```

Docker is only the test bench here; Reprintbuddy itself installs natively on Debian with systemd.
The parts that need no container (package build, both stand-ins read by the production code, the steps on the
pages against a local `reprintbuddy serve`) are ordinary tests in this folder and run with the rest of the suite.
