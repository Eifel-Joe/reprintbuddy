# Installation

Reprintbuddy wird mit einem Befehl nativ auf einem Debian-System installiert. Kein Docker.

## Was du brauchst

Einen Rechner, eine VM oder einen Container (z. B. Proxmox-LXC, unprivilegiert genügt) mit **Debian 13** oder neuer, einen CPU-Kern, 1 GB RAM und 8 GB Platte. Das Übrige steht unter [Voraussetzungen](requirements.md).

## Der Befehl

Als root auf diesem System ausführen:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)"
```

Frische Debian-Systeme haben oft kein curl; der erste Teil installiert es. Das Skript legt das **neueste Release** auf. Der Tag in der Adresse (`v{{ version }}`) ist die Version des Installationsskripts selbst, und sie gehört zu diesem Release. Das Skript fragt nach der Sprache der Oberfläche (Englisch oder Deutsch). Um die Frage zu überspringen, hängst du die Sprache an:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)" _ --language de
```

Das Skript muss vom Tag des Releases stammen, das installiert wird. Tut es das nicht, bricht es ab, wenn sich seine Systemdateien von denen des Releases unterscheiden, und nennt den richtigen Tag; dann führst du es mit diesem Tag anstelle von `<tag>` noch einmal aus:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/<tag>/install/install.sh)"
```

## Was das Installationsskript tut

- installiert die Pakete, die es braucht (`python3`, `python3-venv`, `git`, `curl`, `sudo`, `ca-certificates`),
- legt den Systembenutzer `reprintbuddy` an,
- legt Reprintbuddy auf dem neuesten Release-Tag nach `/opt/reprintbuddy`, mit der Python-Umgebung in `/opt/reprintbuddy/.venv`,
- legt `/var/lib/reprintbuddy` (Modus 700) an, mit Datenbank, Sicherungen und Update-Zustand,
- installiert die systemd-Units `reprintbuddy.service` und `reprintbuddy-update.service`,
- installiert eine sudoers-Regel in `/etc/sudoers.d/reprintbuddy`, mit der der Dienstbenutzer genau diese Befehle als root ausführen darf und sonst nichts (die letzten beiden gehören zum Zurückrollen eines fehlgeschlagenen Updates, das die Datenbank zurückspielt, während der Dienst steht):

    ```text
    /usr/bin/systemctl restart --no-block reprintbuddy.service
    /usr/bin/systemctl restart reprintbuddy.service
    /usr/bin/systemctl start --no-block reprintbuddy-update.service
    /usr/bin/systemctl stop reprintbuddy.service
    /usr/bin/systemctl start reprintbuddy.service
    ```

- installiert den Befehl `/usr/local/bin/reprintbuddy` (siehe [Kommandozeile](../operate/cli.md)),
- startet den Dienst und gibt die Adresse für die Ersteinrichtung aus.

!!! warning "Einrichtung sofort abschließen"
    Öffne jetzt `http://<server-ip>:8080/setup`. Solange es das Admin-Konto nicht gibt, kann es jeder in deinem Netz anlegen. Siehe [Ersteinrichtung](setup.md).

## Netzzugang, den der Rechner braucht

Ausgehend zu den Debian-Paketquellen (`apt`), `github.com` und `raw.githubusercontent.com` (Installation und Updates), `pypi.org` und `files.pythonhosted.org` (Python-Pakete, bei der Installation und bei jedem Update) und zu deinem Bambuddy.

## Das Skript noch einmal ausführen

Das ist unbedenklich. Die installierte Version und die Datenbank bleiben, wie sie sind; es richtet Pakete, Benutzer, Rechte, Units und den Dienst wieder her. Versionen wechseln nur durch das Update, siehe [Update und Sicherung](../operate/update-backup.md). `--dry-run` zeigt, was es tun würde, ohne etwas zu ändern.

## Auf Debian 12

Reprintbuddy braucht Python 3.13 oder neuer; Debian 12 hat nur Python 3.11, deshalb bricht das Installationsskript dort ab. Aktualisiere das System auf Debian 13 (das Debian-Release-Upgrade) und führe den Installationsbefehl danach erneut aus. Er erkennt, dass die Python-Umgebung nicht mehr zum System-Python passt, baut sie neu und behält die installierte Version, die Datenbank und alle Einstellungen.

Eine auf Debian 12 gemachte Installation kann vorher nicht auf diese Version aktualisieren: das Update scheitert mit der Meldung von pip („requires a different Python“), wird zurückgerollt, und die laufende Version bleibt. Zieh vorher eine Sicherung, siehe [Update und Sicherung](../operate/update-backup.md).

## Für die Entwicklung

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m reprintbuddy serve --db ./dev.db
```
