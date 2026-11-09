# Installation

Reprintbuddy installs natively on a Debian system with one command. No Docker.

## What you need

A computer, a VM or a container (for example a Proxmox LXC, unprivileged is fine) with **Debian 13** or newer, one CPU core, 1 GB RAM and 8 GB disk. See [Requirements](requirements.md) for the rest.

## The command

Run it as root on that system:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)"
```

Fresh Debian systems often come without curl; the first part installs it. The tag in the address (`v{{ version }}`) is the version that gets installed, so installer and code are guaranteed to belong together. The installer asks for the language of the interface (English or German). To skip the question, add the language:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)" _ --language de
```

To install another version, put its tag into the address in place of `<tag>`:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/<tag>/install/install.sh)"
```

## What the installer does

- installs the packages it needs (`python3`, `python3-venv`, `git`, `curl`, `sudo`, `ca-certificates`),
- creates the system user `reprintbuddy`,
- puts Reprintbuddy into `/opt/reprintbuddy` on the newest release tag, with its Python environment in `/opt/reprintbuddy/.venv`,
- creates `/var/lib/reprintbuddy` (mode 700) with the database, the backups and the update state,
- installs the systemd units `reprintbuddy.service` and `reprintbuddy-update.service`,
- installs a sudoers rule in `/etc/sudoers.d/reprintbuddy` that lets the service user run exactly these commands as root, and nothing else (the last two are for the rollback of a failed update, which restores the database while the service is stopped):

    ```text
    /usr/bin/systemctl restart --no-block reprintbuddy.service
    /usr/bin/systemctl restart reprintbuddy.service
    /usr/bin/systemctl start --no-block reprintbuddy-update.service
    /usr/bin/systemctl stop reprintbuddy.service
    /usr/bin/systemctl start reprintbuddy.service
    ```

- installs the command `/usr/local/bin/reprintbuddy` (see [Command line](../operate/cli.md)),
- starts the service and prints the address for the first-run setup.

!!! warning "Finish the setup right away"
    Open `http://<server-ip>:8080/setup` now. Until the admin account exists, anyone in your network can create it. See [First setup](setup.md).

## Network access the machine needs

Outgoing access to the Debian package sources (`apt`), `github.com` and `raw.githubusercontent.com` (installation and updates), `pypi.org` and `files.pythonhosted.org` (Python packages, at installation and at every update) and to your Bambuddy.

## Running the installer again

This is safe. The installed version and the database stay as they are; it repairs packages, user, permissions, units and the service. Versions change only through the update, see [Update and backup](../operate/update-backup.md). `--dry-run` shows what it would do without changing anything.

## On Debian 12

Reprintbuddy needs Python 3.13 or newer; Debian 12 only has Python 3.11, so the installer refuses to run there. Upgrade the system to Debian 13 (the Debian release upgrade), then run the installation command again. It notices that the Python environment no longer fits the system's Python, builds it anew and keeps the installed version, the database and all settings.

An installation made on Debian 12 cannot update to this version before that: the update fails with pip's message "requires a different Python", is rolled back, and the running version stays. Take a backup first, see [Update and backup](../operate/update-backup.md).

## For development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m reprintbuddy serve --db ./dev.db
```
