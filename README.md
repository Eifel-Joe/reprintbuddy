<p align="center">
  <img src="src/reprintbuddy/web/static/img/logo.svg" alt="Reprintbuddy" width="128" height="128">
</p>

<h1 align="center">Reprintbuddy</h1>

<p align="center">
  <strong>Your shelf stays stocked. Bambuddy does the printing.</strong><br>
  Self-hosted stock keeping for 3D-printed items, with reprints through Bambuddy's queue.
</p>

<p align="center">
  <a href="https://github.com/Eifel-Joe/reprintbuddy/releases"><img src="https://img.shields.io/github/v/release/Eifel-Joe/reprintbuddy" alt="Release"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Eifel-Joe/reprintbuddy/ci.yml?branch=main&logo=github&label=CI" alt="CI"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Eifel-Joe/reprintbuddy" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/stargazers"><img src="https://img.shields.io/github/stars/Eifel-Joe/reprintbuddy?style=flat" alt="Stars"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/issues"><img src="https://img.shields.io/github/issues/Eifel-Joe/reprintbuddy" alt="Issues"></a>
</p>

[Deutsch: README.de.md](README.de.md)

Reprintbuddy keeps the stock of 3D-printed items. It reprints what runs low by
handing print jobs to Bambuddy's queue,
lets you check finished prints, and tells you what to reorder. It runs as a
small web application on your home network, reachable from outside only through a VPN.

> **Status:** under development, not ready for real use yet.

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

- Bambuddy 1.3 or newer, with its own spool management or with Spoolman (**Spoolman is
  optional**). Reprintbuddy reads the filament stock through Bambuddy and never talks to
  Spoolman itself.
- **Print files stored directly in Bambuddy's file manager**, uploaded and sliced
  (`.gcode.3mf`). Linked external folders are not enough: Bambuddy keeps no checksum for
  their files and does not re-read plates and filament when it rescans them, so a changed
  version would go unnoticed.
- A **dedicated Bambuddy user** `reprintbuddy` in its **own Bambuddy group** with the rights
  listed under *First run*, and an **API key** of that user. The dedicated user makes
  Reprintbuddy's own jobs recognisable in Bambuddy. The setup guides you through it.
- A **Proxmox LXC container** (unprivileged is fine) or another machine with **Debian 12 or 13**.
  One CPU core, 1 GB RAM and 8 GB disk are enough. No Docker needed.
- Outgoing network access from that machine to the Debian package sources (`apt`),
  `github.com` and `raw.githubusercontent.com` (installation and updates),
  `pypi.org` and `files.pythonhosted.org` (Python packages, at installation and at
  every update) and to your Bambuddy.

## Installation

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v0.3.4/install/install.sh)"
```

Run it as root inside the container. Fresh Debian containers often come without curl; the first part installs it.
The tag in the address (`v0.3.4`) is the version that gets installed; installer and code are guaranteed to belong
together. To skip the language question, add the language:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v0.3.4/install/install.sh)" _ --language de
```

To install another version, put its tag into the address, for example `v0.3.4` for `<tag>`:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/<tag>/install/install.sh)"
```

What the installer does:

- installs the packages it needs (`python3`, `python3-venv`, `git`, `curl`, `sudo`,
  `ca-certificates`),
- creates the system user `reprintbuddy`,
- puts Reprintbuddy into `/opt/reprintbuddy` on the newest release tag, with its Python
  environment in `/opt/reprintbuddy/.venv`,
- creates `/var/lib/reprintbuddy` (mode 700) with the database, the backups and the update state,
- installs the systemd units `reprintbuddy.service` and `reprintbuddy-update.service`,
- installs a sudoers rule in `/etc/sudoers.d/reprintbuddy` that lets the service user run
  exactly these commands as root, and nothing else (the last two are for the rollback of a failed
  update, which restores the database while the service is stopped):

  ```text
  /usr/bin/systemctl restart --no-block reprintbuddy.service
  /usr/bin/systemctl restart reprintbuddy.service
  /usr/bin/systemctl start --no-block reprintbuddy-update.service
  /usr/bin/systemctl stop reprintbuddy.service
  /usr/bin/systemctl start reprintbuddy.service
  ```

- installs the command `/usr/local/bin/reprintbuddy` (see *Command line*),
- starts the service and prints the address for the first-run setup.

**Finish the setup right away** at `http://<container-ip>:8080/setup`: until the admin
account exists, anyone in your network can create it.

Running the installer again is safe: the installed version and the database stay as they
are; it repairs packages, user, permissions, units and the service. Versions change only
through the update (see *Update*). `--dry-run` shows what it would do without changing anything.

For development:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m reprintbuddy serve --db ./dev.db
```

## First run

Open the address printed by the installer. Until you finish the setup, the
page is open to everyone on your network, so do it right away. During first-run setup,
open Reprintbuddy by its IP address (or a local name); names from public domains, e.g. VPN
DNS names, are accepted only after setup.

1. **Admin account:** choose a user name, a password and the language.
2. **Group and user in Bambuddy:** in Bambuddy, under *Settings → Users*, create your own
   group named `Reprintbuddy`. No system group fits: *Operators* only sees its own files and
   queue entries, *Administrators* may do everything. Give the group these rights (the
   internal names are in brackets). Bambuddy shows its rights in English only, so they are
   named here as in Bambuddy:

   - Read All Library (`library:read_all`): so that Reprintbuddy sees your print files
   - Read All Queue (`queue:read_all`): so that Reprintbuddy sees the whole queue
   - Create Queue (`queue:create`): so that Reprintbuddy can queue reprints
   - Print Without Review (`queue:start_unreviewed`): so that reprints start without approval
   - Read Printers (`printers:read`): so that Reprintbuddy sees the printers and their state
   - Read Inventory (`inventory:read`): so that Reprintbuddy reads the filament stock
   - Read Filaments (`filaments:read`): to tell whether Bambuddy keeps the spools itself or in
     Spoolman

   Only to create the key in the next step, the group also needs these three rights; Reprintbuddy
   does not check them, and they may be removed again afterwards: Read Api Keys
   (`api_keys:read`), Create Api Keys (`api_keys:create`) and Read Settings (`settings:read`;
   without it Bambuddy hides the Settings menu).

   Then create the user `reprintbuddy` in this group. Its password is needed only once, to
   create the key; Reprintbuddy neither asks for it nor stores it.
3. **API key:** sign out of Bambuddy and sign in as `reprintbuddy` (a key always belongs to
   the user who is signed in). Open *Settings → API keys → Create key* and set it exactly like this:
   name "Reprintbuddy"; *Read Status* **on**; *Manage Queue* **on**; all others **off**
   (*Control Printer*, *Manage Library*, *Manage Inventory*, *Manage Maintenance*,
   *Manage Archives*, *Manage Projects*, *Allow Cloud Access*, *Update Electricity Price*,
   *Send Notifications*). Copy the key at once (Bambuddy shows it only one time) and sign out again.
4. **Connect Bambuddy:** enter the Bambuddy address and the key. Reprintbuddy asks Bambuddy
   (`GET /api/v1/auth/me`) whom the key belongs to and which rights it can really use, and
   shows the owner and each required right with a check or a cross. The interface opens only
   when no right is missing. A key that belongs to an administrator is accepted, with a
   warning that it may do more than needed. The same check is under *Settings*.

   The two fields are marked so that password managers do not fill in your own credentials.

## Settings

There is **no configuration file**. Every setting lives in the database and is
changed under *Settings* in the web interface: language, Bambuddy connection,
password, listen address, port, optional HTTPS certificate, restart, update and
export. The only thing
given on the command line is the database path (`--db`).

## Update

*Settings → Update*: press **Check for updates**; if a newer release exists, press
**Update to …**. *Available from the first release.* The update

1. backs up the database,
2. switches to the new release tag,
3. installs the packages,
4. migrates the database,
5. restarts the service and
6. checks that the new version answers.

If anything fails, Reprintbuddy goes back to the previous version **and** restores the
database from the backup; the page says which step failed. Changes made while the update
runs would be lost in that case, so every change is paused until it has finished.

If the page says **Update was interrupted** or **Update failed and the previous version could not be restored**,
run the installer again: it repairs packages, units and the service and keeps the database. If Reprintbuddy still
does not start, restore the backup named on the page (see *Backup and export*) and run the installer again.
The page shows the Git tag and, if it differs, the package version next to it.

Backups are kept in `/var/lib/reprintbuddy/backups/` (the last 5; a backup named by a failed rollback is never deleted). To start an update by hand:
`systemctl start reprintbuddy-update.service`; its log is `journalctl -u reprintbuddy-update`.

Versions are git tags `vX.Y.Z`, and the package version equals the tag. If a release changes the
systemd units or the sudoers rule, its release notes say so; then run the installer again.

## Restart

*Settings → Restart* restarts Reprintbuddy. Address, port and HTTPS take effect only after a
restart; the settings page says when a saved change is still waiting. After a change of
address or port the restart page names the new address. In the container:
`systemctl restart reprintbuddy`.

## HTTPS

HTTP is fine inside your home network or over a VPN. For HTTPS, upload a certificate and its private key
(without a password) under *Settings*; Reprintbuddy checks that they belong together before it
saves them, then restart. To go back to HTTP, tick *Remove certificate and private key*.

Alternatively run Reprintbuddy behind a reverse proxy you already have: Reprintbuddy stays on
HTTP and the proxy does TLS. If the proxy runs in the same container, set the listen address
to `127.0.0.1`.

Locked out by a certificate that stopped working (expired, wrong name)? In the container:

```bash
reprintbuddy disable-https && systemctl restart reprintbuddy
```

A wrong port: `reprintbuddy set-port 8080 && systemctl restart reprintbuddy`.

## Backup and export

Everything lives under `/var/lib/reprintbuddy`, so a Proxmox backup of the container covers it.
*Settings → Export* downloads all data as a JSON file, **without** API keys, passwords, the
private HTTPS key and the session secret.

To restore a database backup by hand:

```bash
systemctl stop reprintbuddy
runuser -u reprintbuddy -- cp /var/lib/reprintbuddy/backups/<backup>.db /var/lib/reprintbuddy/reprintbuddy.db
rm -f /var/lib/reprintbuddy/reprintbuddy.db-wal /var/lib/reprintbuddy/reprintbuddy.db-shm
systemctl start reprintbuddy
```

## Security

- Do not expose Reprintbuddy directly to the internet (no port forwarding, no public
  reverse proxy). For remote access, use a VPN into your network (e.g. your router's VPN,
  WireGuard or Tailscale).
- API keys, the private key of an HTTPS certificate and passwords are only
  visible while you type them. Once saved, the interface shows `********`;
  an empty field keeps the stored value, a new entry replaces it. Passwords are
  stored only as hashes.
- Every change needs a signed-in session and a CSRF token.
- The database file is readable only by the service user. Anyone who is root on
  the machine can read it.

Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Command line

In the container the command `reprintbuddy` runs as the service user `reprintbuddy` (also when
you are root), so database files keep their owner:

```bash
reprintbuddy set-password --username <name>
reprintbuddy set-port <port>
reprintbuddy set-host <address>
reprintbuddy disable-https
reprintbuddy --version
```

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
