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

- For larger changes, **open an issue first** and agree on the approach.
- **One topic per pull request.**
- **Add tests** for new behaviour, and make sure **all tests pass**.
- No keys, passwords or personal data, not even in tests or examples. Use
  neutral example data.
- Update the **documentation in both languages** (English and German). The README
  and the pages under `docs/` go into the **same pull request** when installation,
  setup or use changes; the pull request template asks for it.

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
its header), so a changed interface means running it again.

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
Settings are not read from files or environment variables: every setting lives
in the database and is edited in the web interface.

## Handling issues and pull requests

Pull requests are merged only after the maintainer has approved them.
