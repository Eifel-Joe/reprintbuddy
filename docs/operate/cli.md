# Command line

The command `reprintbuddy` is the way back in when the web interface is out of reach: a forgotten password, a wrong port, a wrong listen address, a certificate that stopped working, or a backup to restore. This page lists the commands.

## How the command runs

The installer puts the command at `/usr/local/bin/reprintbuddy`. Run it on the machine that runs Reprintbuddy, as root. It then runs as the service user `reprintbuddy` (from the folder `/`), so the database files keep their owner. Commands that ask for a password (`set-password`, `restore-backup`) keep your terminal; the others run without one.

Every command takes `--db PATH`, the database file. The default is `/var/lib/reprintbuddy/reprintbuddy.db`, the database of the installed service, so you leave it out. It is the only thing that does not live in the database itself, see [Settings](settings.md). What a command changes (for example a new password or port) is written to the [Log](log.md) as done by the **User**, with the note "command on the server".

## Commands for you

| Command | What it does |
|---|---|
| `reprintbuddy --version` | Prints the installed version, for example `reprintbuddy 0.3.12`. |
| `reprintbuddy set-password --username <name>` | Sets the password of the account. Asks for the new password twice on the terminal. |
| `reprintbuddy set-port <port>` | Sets the port, 1 to 65535. |
| `reprintbuddy set-host <address>` | Sets the listen address, for example `0.0.0.0`. Refused if this machine cannot listen on it. |
| `reprintbuddy disable-https` | Removes the HTTPS certificate and key: Reprintbuddy serves plain HTTP again. |
| `reprintbuddy restore-backup <file>` | Restores a full backup while the service is stopped. See [Update and backup](update-backup.md#restore-on-the-command-line). |

`set-port`, `set-host` and `disable-https` change the stored setting. They take effect after a restart:

```bash
systemctl restart reprintbuddy
```

## Forgotten password

```bash
reprintbuddy set-password --username admin
```

The command asks for "New password:" and "Repeat password:" (nothing is shown while you type). The password needs at least 8 characters. It ends with "Password set for admin. All sessions have ended." and everybody signs in again. You need no restart.

There is only one account, and `set-password` also **renames** it to the given `--username`. So a forgotten user name is no obstacle: use the name you want from now on. If there is no account yet, the command creates it.

## Wrong port

```bash
reprintbuddy set-port 8080
systemctl restart reprintbuddy
```

For a port below 1024 the command adds a note: the service set up by the installer may use such a port, a service started another way may not. If Reprintbuddy does not start after the restart, choose a higher port.

## Wrong listen address

```bash
reprintbuddy set-host 0.0.0.0
systemctl restart reprintbuddy
```

`0.0.0.0` means every address of the machine. The command refuses an address this machine cannot listen on ("host: this machine cannot listen on that address") and saves nothing.

## Certificate stopped working

```bash
reprintbuddy disable-https && systemctl restart reprintbuddy
```

Afterwards open Reprintbuddy with `http://`. You can upload a new certificate under *Settings → General*, see [Network and security](network.md#https).

## Commands of the installer and the service

You normally never type these.

| Command | Used for |
|---|---|
| `reprintbuddy serve` | Runs the web interface. The service `reprintbuddy.service` starts it. |
| `reprintbuddy migrate` | Creates or updates the database. |
| `reprintbuddy init --language en` (or `de`) | Migrates and sets the interface language. The installer uses it. |
| `reprintbuddy update [--target <tag>]` | The update helper. The update unit starts it; to update by hand use `systemctl start reprintbuddy-update.service`, see [Update and backup](update-backup.md#update-by-hand). |
| `reprintbuddy url [--host <host>] [--next]` | Prints the address of the web interface; with `--next` it adds the page to open next. The installer uses it. |

## Errors

A command that is asked wrongly (a port that is not a number, two passwords that differ, an address the machine cannot use) prints `reprintbuddy: …` on stderr, changes nothing and ends with exit code 2.

## Related pages

- [Network and security](network.md): locked out by HTTPS, port or address.
- [Update and backup](update-backup.md): `restore-backup` in detail.
- [Settings](settings.md): the same settings in the interface.
