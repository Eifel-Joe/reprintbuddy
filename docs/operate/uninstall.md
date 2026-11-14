# Uninstall

Reprintbuddy has no uninstall command, and the web interface cannot remove it. You take it off the system by hand, as root, in the steps below. Run each command on its own. Nothing outside the system that ran Reprintbuddy is touched; the last section names what stays behind in Bambuddy.

## 1. Keep your data (only if you want it)

Step 5 deletes the data folder with the database **and the backups kept there**. If you may want your articles, settings and log again, download a backup first and keep it **outside** this system: in Reprintbuddy under *Settings → Update and backup*, **Full backup**, with a password (see [Update and backup](update-backup.md)). After a new installation and its first setup, the same page restores it. Skip this step if you want to start from nothing.

## 2. Stop the services and switch them off

The first command stops an update that happens to be running; the second stops Reprintbuddy and takes it out of the start at boot.

```bash
systemctl stop reprintbuddy-update.service
```

```bash
systemctl disable --now reprintbuddy.service
```

## 3. Remove the unit files, the sudoers rule and the command

The two systemd units, the rule that lets the service restart itself, and the command `reprintbuddy`:

```bash
rm /etc/systemd/system/reprintbuddy.service
```

```bash
rm /etc/systemd/system/reprintbuddy-update.service
```

```bash
rm /etc/sudoers.d/reprintbuddy
```

```bash
rm /usr/local/bin/reprintbuddy
```

## 4. Let systemd forget the units

```bash
systemctl daemon-reload
```

## 5. Delete the program and the data

The program folder `/opt/reprintbuddy` holds the installed versions and the Python environment; the data folder `/var/lib/reprintbuddy` holds the database, the backups and the update state. **This cannot be undone.**

```bash
rm -rf /opt/reprintbuddy
```

```bash
rm -rf /var/lib/reprintbuddy
```

## 6. Remove the service user and its group

The installer created the system user `reprintbuddy` together with a group of the same name. `userdel` normally takes the group along; the second command removes it if it is still there (it does nothing otherwise).

```bash
userdel reprintbuddy
```

```bash
getent group reprintbuddy && groupdel reprintbuddy
```

## 7. Check

The list should be empty (`0 unit files listed`):

```bash
systemctl list-unit-files 'reprintbuddy*'
```

## What stays

- The Debian packages the installer added (`python3`, `python3-venv`, `curl`, `sudo`, `ca-certificates`). Other programs may use them, so they are not removed here. Remove one yourself only if you know that nothing else on the system needs it.
- Older lines in the system log (`journalctl -u reprintbuddy`) until the journal rotates them.
- **In Bambuddy:** the user `reprintbuddy`, its group `Reprintbuddy` and its API key (see [First setup](../start/setup.md)). If you no longer need them, delete them there, under *Settings → Users* and *Settings → API keys*. The print jobs Reprintbuddy queued and the print files you uploaded through it stay in Bambuddy like any other.
