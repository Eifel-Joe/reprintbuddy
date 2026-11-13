# Installation

Reprintbuddy wird mit einem Befehl nativ auf einem Debian-System installiert. Kein Docker.

## Was du brauchst

Einen Rechner, eine VM oder einen Container (z. B. Proxmox-LXC, unprivilegiert genügt) mit **Debian 13** oder neuer, einen CPU-Kern, 1 GB RAM und 8 GB Platte. Das Übrige steht unter [Voraussetzungen](requirements.md).

## Der Befehl

Als root auf diesem System ausführen:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)"
```

Frische Debian-Systeme haben oft kein curl; der erste Teil installiert es. Das Skript legt das **Release auf, dessen Tag in der Adresse steht** (hier `v{{ version }}`): Skript und Release sind eine Version. Das Skript fragt nach der Sprache der Oberfläche (Englisch oder Deutsch). Um die Frage zu überspringen, hängst du die Sprache an:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{{ version }}/install/install.sh)" _ --language de
```

Willst du eine andere Version installieren, setzt du ihren Tag anstelle von `<tag>` in die Adresse (ab v0.3.14; die Skripte früherer Tags legen das neueste Release auf). Mehr braucht es nicht: eine Option für die Version gibt es nicht. Ist Reprintbuddy schon installiert, behält das Skript diese Version und bessert nur den Rest aus; unterscheiden sich die Systemdateien des Skripts von denen der installierten Version, bricht es ab und nennt den Tag, den du nehmen musst:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/<tag>/install/install.sh)"
```

## Was das Installationsskript tut

- installiert die Pakete, die es braucht (`python3`, `python3-venv`, `curl`, `sudo`, `ca-certificates`),
- legt den Systembenutzer `reprintbuddy` an,
- lädt das Paket des Release des Skripts von GitHub (`reprintbuddy-<tag>.tar.gz`) mit seiner Prüfsummen-Datei, installiert nichts, wenn beides nicht zusammenpasst, und entpackt es nach `/opt/reprintbuddy/releases/<tag>`; `/opt/reprintbuddy/release.json` nennt die installierte Version. Die Python-Umgebung liegt in `/opt/reprintbuddy/.venv`. git braucht es nicht,
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

Ausgehend zu den Debian-Paketquellen (`apt`), `github.com`, `raw.githubusercontent.com` und `release-assets.githubusercontent.com` (Installation und Updates: das Paket eines Release liefert GitHub vom letzten aus), `api.github.com` (die Versionshinweise auf der Update-Karte), `pypi.org` und `files.pythonhosted.org` (Python-Pakete, bei der Installation und bei jedem Update) und zu deinem Bambuddy.

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
