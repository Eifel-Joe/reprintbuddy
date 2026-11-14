# Installation

Reprintbuddy installs natively on a Debian system with one command. No Docker.

## What you need

A computer, a VM or a container (for example a Proxmox LXC, unprivileged is fine) with **Debian 13** or newer, one CPU core, 1 GB RAM and 8 GB disk. See [Requirements](requirements.md) for the rest.

## The command

Run it as root on that system:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)"
```

Fresh Debian systems often come without curl; the first part installs it. The installer puts the **release whose tag is in the address** in place (`v{{ version }}` here): script and release are one version. The installer asks for the language of the interface (English or German). To skip the question, add the language:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)" _ --language de
```

To install another version, put its tag in the address in place of `<tag>` (from v0.3.14 on; the scripts of earlier tags put the newest release in place). The address of a tag is all it takes: there is no option for the version. If Reprintbuddy is already installed, the installer keeps that version and only repairs the rest; when the system files of the script differ from those of the installed version, it stops with a message that names the tag to use:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/<tag>/install/install.sh)"
```

## What the installer does

- installs the packages it needs (`python3`, `python3-venv`, `curl`, `sudo`, `ca-certificates`),
- creates the system user `reprintbuddy`,
- downloads the package of the release of the script from GitHub (`reprintbuddy-<tag>.tar.gz`) with its checksum file, installs nothing if they do not match, and unpacks it to `/opt/reprintbuddy/releases/<tag>`; `/opt/reprintbuddy/release.json` names the installed version. The Python environment is in `/opt/reprintbuddy/.venv`. git is not needed,
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

Outgoing access to the Debian package sources (`apt`), `github.com`, `raw.githubusercontent.com` and `release-assets.githubusercontent.com` (installation and updates: GitHub serves the package of a release from the last one), `api.github.com` (the release notes on the update card), `pypi.org` and `files.pythonhosted.org` (Python packages, at installation and at every update) and to your Bambuddy.

## Running the installer again

This is safe. The installed version and the database stay as they are; it repairs packages, user, permissions, units and the service. If the unpacked folder of the installed version (`/opt/reprintbuddy/releases/<tag>`) is missing, the installer of that version downloads its package again, checked as at the installation; the installer of another version stops and names the command of the installed one. Versions change only through the update, see [Update and backup](../operate/update-backup.md). `--dry-run` shows what it would do without changing anything.

## Removing Reprintbuddy

There is no uninstall command in the installer. The steps to take Reprintbuddy off the system again, with or without keeping your data, are on the page [Uninstall](../operate/uninstall.md).

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
