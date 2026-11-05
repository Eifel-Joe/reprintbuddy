# Reprintbuddy

[Deutsch: README.de.md](README.de.md)

Reprintbuddy keeps the stock of 3D-printed items. It reprints what runs low by
handing print jobs to Bambuddy's queue,
lets you check finished prints, and tells you what to reorder. It runs as a
small web application on your home network.

> **Status:** under development, no release yet.

Reprintbuddy is an independent project and is not affiliated with Bambuddy or Bambu Lab.

## What it is, and what it is not

- **It is** a stock book per article with minimum stock and buffer, its own
  reprint queue, a check list for finished prints, assembly bookings and
  reorder suggestions for filament and bought-in parts.
- **It never controls printers.** Bambuddy stays your print system. Reprintbuddy
  only adds jobs to Bambuddy's queue and reads status. It does not start,
  stop or pause anything, and it never writes to Spoolman.
- It contains no articles of its own: you create articles, sets, parts and
  rules in the web interface.

## Requirements

- Bambuddy 1.3 or newer, running in **Spoolman mode** (Reprintbuddy reads filament
  stock through Bambuddy).
- A **dedicated Bambuddy user** for Reprintbuddy (for example `reprintbuddy`) with the
  permission **Print without review**, so queued prints start without
  confirmation, and an **API key** for that user with the permissions
  **Read Status** and **Manage Queue**. The dedicated user makes Reprintbuddy's
  own jobs recognisable in Bambuddy.
- A Linux machine or container with Python 3.11 or newer. No Docker needed.

## Installation

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<owner>/reprintbuddy/main/install/install.sh)"
```

The installer sets up a Python environment and a systemd service, asks for the
interface language (English is the default) and prints the address for the
first-run setup. *Available from the first release.*

For development:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m reprintbuddy serve --db ./dev.db
```

## First run

Open the address printed by the installer. Until you finish the setup, the
page is open to everyone on your network, so do it right away.

1. **Admin account:** choose a user name, a password and the language.
2. **Connect Bambuddy:** enter the Bambuddy address, the user name and the API
   key, and confirm that the user has the permission *Print without review*.
   Reprintbuddy checks the connection before it opens the interface.

## Settings

There is **no configuration file**. Every setting lives in the database and is
changed under *Settings* in the web interface: language, Bambuddy connection,
password, listen address, port and optional HTTPS certificate. The only thing
given on the command line is the database path (`--db`).

## Update

*Settings → Update* shows the installed and the latest released version and
updates on a click, after backing up the database. *Available from the first
release.*

## Security

- Reprintbuddy is meant for your **home network only**. Do not expose it to the
  internet.
- API keys, the private key of an HTTPS certificate and passwords are only
  visible while you type them. Once saved, the interface shows `********`;
  an empty field keeps the stored value, a new entry replaces it. Passwords are
  stored only as hashes.
- Every change needs a signed-in session and a CSRF token.
- The database file is readable only by the service user. Anyone who is root on
  the machine can read it.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Forgotten password or wrong port

On the machine running Reprintbuddy:

```bash
reprintbuddy set-password --username admin
reprintbuddy set-port 8080
reprintbuddy set-host 0.0.0.0
```

`set-password` also renames the account to the given `--username`, so a forgotten
user name is no obstacle.

Restart the service afterwards (`set-port` and `set-host` need it to take effect).
`set-host` sets the listen address (for example `0.0.0.0`) if a wrong one locked you out.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Reprintbuddy is licensed under the GNU Affero General Public License, version 3 or
later (AGPL-3.0-or-later). See [LICENSE](LICENSE).

Copyright (C) 2026 Eifel-Joe
