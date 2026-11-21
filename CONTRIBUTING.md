# Contributing

[Deutsch: CONTRIBUTING.de.md](CONTRIBUTING.de.md)

Thank you for helping. Issues and pull requests may be written in English or
German.

## Where things go

- **Bugs, feature requests and questions:** open an issue and pick the matching
  template. Questions are welcome too; say so in the title.
- **Security vulnerabilities:** never as a public issue. See
  [SECURITY.md](SECURITY.md).

## A good bug report

Please include (the bug template asks for each of these):

- the **Reprintbuddy version** and the **Bambuddy version**,
- whether you use **Spoolman** (yes or no),
- the **steps** to reproduce,
- what you **expected** and what **actually** happened,
- an excerpt of the log, with **API keys and passwords removed**.

## What will not be accepted

- **Printer control in Reprintbuddy.** Bambuddy prints; Reprintbuddy only queues
  jobs and reads status.
- A dependency on **Docker** or on **cloud services**. Reprintbuddy installs
  natively and runs on your own network.

## Pull requests

- **Open the pull request against the branch `dev`**, not `main` (see *Branches and versions*).
- For larger changes, **open an issue first** and agree on the approach.
- **One topic per pull request.**
- **Add tests** for new behaviour, and make sure **all tests pass**.
- No keys, passwords or personal data, not even in tests or examples. Use
  neutral example data.
- Update the **documentation in both languages** (English and German). The README
  and the pages under `docs/` go into the **same pull request** when installation,
  setup or use changes; the pull request template asks for it.
- A **new database step** (an Alembic revision under `src/reprintbuddy/migrations/versions/`) may only add:
  new tables, new fields that are empty or have a default (added without rebuilding the table), and
  indexes on new fields or new tables. It brings its downgrade, which removes exactly that, and carries
  two marks: `return_note = {"de": "…", "en": "…"}` (what it adds, shown when a pre-release goes back
  to the stable version) and `rework = False`. A step that must change existing data is marked
  `rework = True`, and the release notes of the pre-release that brings it begin with the line
  `**Database rework:** …`; going back then restores the backup from before the update. The CI checks
  every step after the last stable version against this rule (`tests/test_migration_rules.py`).

## Branches and versions

- **Pull requests go to `dev`.** The branch `main` holds only what has been released as a stable version.
- A stable version `vX.Y.Z` is made from `dev` in steps: first as one or more pre-releases `vX.Y.Z-beta.N`, which
  can be tried out, then the tested state reaches `main` by a pull request and is released as `vX.Y.Z`. New features
  therefore come as pre-releases first.
- To try a pre-release, switch on **Include pre-releases** under *Settings → Update and backup* in Reprintbuddy.
  Switched off again, an installed pre-release stays until a newer stable version comes, or until you go
  back to the stable version from the update card.

## Documentation

The pages under `docs/` are built with MkDocs and published on GitHub Pages. Every
page exists twice: `page.md` in English and `page.de.md` in German next to it.

```bash
.venv/bin/pip install -e ".[docs]"
.venv/bin/mkdocs serve           # preview in the browser
.venv/bin/mkdocs build --strict  # what the CI runs: a broken link fails
```

Screenshots show invented example data only, never real articles, names or
addresses. `tools/docs_screenshots.py` takes them from a test instance (usage in
its header), so a changed interface means running it again. The pictures show
a fixed stand-in version (`9.8.7`), not the installed one, so a new release alone
never calls for new pictures.

## License of contributions

By submitting a contribution you license it under **AGPL-3.0-or-later**, the
license of this project, and you additionally allow the maintainer (Eifel-Joe)
to distribute it under other license terms as well.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Bambuddy is never contacted by the tests; it is imitated with `respx`.

The suite has thousands of small tests. A plain `pytest` runs them one after the other. The CI
runs them on four processes (`pytest -n 4 --dist loadfile -m "not serial"`, from `pytest-xdist`) and then
the tests marked `serial` on their own (`pytest -m serial`). A test that depends on the wall clock, that
is, one that checks how long something took or counts ticks within fixed sleeps, gets
`@pytest.mark.serial` (a whole file: `pytestmark = pytest.mark.serial`), so that busy workers cannot
make it fail; `tests/test_ci_split.py` fails for an unmarked test with a bound on its running time.

Every pull request also runs the job **End-to-end (Debian 13)** (`.github/workflows/e2e.yml`): in a Debian 13
container with systemd it installs the last stable version with its installer, updates it from the settings page
to the state of the pull request (built as the pre-release after it), restores a backup through the page and the
command line, goes back to the stable version twice stopped from outside and once to the end, and checks version,
data, the card of the update and the log after each step. GitHub and Bambuddy are stand-ins inside the container
(`tests/system/`), so the job needs no secret and no network of your own. On Linux with Docker you can start it
yourself with `bash tests/system/run.sh` (after `pip install .`). Docker is only the test bench on the runner and
not a dependency of Reprintbuddy, which still installs natively on Debian with systemd.

Settings are not read from files or environment variables: every setting lives
in the database and is edited in the web interface.

## Handling issues and pull requests

Pull requests are merged only after the maintainer has approved them.
