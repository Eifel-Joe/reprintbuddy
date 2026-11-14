# Deinstallation

Reprintbuddy hat keinen Befehl zum Deinstallieren, und die Weboberfläche kann es nicht entfernen. Du nimmst es als root von Hand vom System, in den Schritten unten. Führe jeden Befehl einzeln aus. Außerhalb des Systems, auf dem Reprintbuddy lief, wird nichts angefasst; der letzte Abschnitt nennt, was in Bambuddy zurückbleibt.

## 1. Daten behalten (nur wenn du sie willst)

Schritt 5 löscht den Datenordner mit der Datenbank **und den dort liegenden Sicherungen**. Wenn du deine Artikel, Einstellungen und das Protokoll vielleicht noch einmal brauchst, lade vorher eine Sicherung herunter und bewahre sie **außerhalb** dieses Systems auf: in Reprintbuddy unter *Einstellungen → Update und Sicherung* die **Vollständige Sicherung**, mit Passwort (siehe [Update und Sicherung](update-backup.md)). Nach einer neuen Installation und ihrer Ersteinrichtung spielt dieselbe Seite sie wieder ein. Überspring den Schritt, wenn du bei null anfangen willst.

## 2. Dienste stoppen und abschalten

Der erste Befehl hält ein Update an, das gerade laufen sollte; der zweite stoppt Reprintbuddy und nimmt es aus dem Start beim Hochfahren.

```bash
systemctl stop reprintbuddy-update.service
```

```bash
systemctl disable --now reprintbuddy.service
```

## 3. Dienstdateien, sudo-Regel und Befehl entfernen

Die beiden systemd-Dienste, die Regel, mit der sich der Dienst selbst neu starten darf, und der Befehl `reprintbuddy`:

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

## 4. systemd die Dienste vergessen lassen

```bash
systemctl daemon-reload
```

## 5. Programm und Daten löschen

Der Programmordner `/opt/reprintbuddy` enthält die installierten Versionen und die Python-Umgebung; der Datenordner `/var/lib/reprintbuddy` die Datenbank, die Sicherungen und den Update-Stand. **Das lässt sich nicht rückgängig machen.**

```bash
rm -rf /opt/reprintbuddy
```

```bash
rm -rf /var/lib/reprintbuddy
```

## 6. Dienstbenutzer und Gruppe entfernen

Der Installer hat den Systembenutzer `reprintbuddy` zusammen mit einer gleichnamigen Gruppe angelegt. `userdel` nimmt die Gruppe normalerweise mit; der zweite Befehl entfernt sie, falls sie noch da ist (sonst tut er nichts).

```bash
userdel reprintbuddy
```

```bash
getent group reprintbuddy && groupdel reprintbuddy
```

## 7. Prüfen

Die Liste sollte leer sein (`0 unit files listed`):

```bash
systemctl list-unit-files 'reprintbuddy*'
```

## Was bleibt

- Die Debian-Pakete, die der Installer nachinstalliert hat (`python3`, `python3-venv`, `curl`, `sudo`, `ca-certificates`). Andere Programme können sie nutzen, deshalb werden sie hier nicht entfernt. Entferne eines nur selbst, wenn du weißt, dass nichts anderes auf dem System es braucht.
- Ältere Zeilen im Systemprotokoll (`journalctl -u reprintbuddy`), bis das Journal sie rotiert.
- **In Bambuddy:** der Benutzer `reprintbuddy`, seine Gruppe `Reprintbuddy` und sein API-Schlüssel (siehe [Ersteinrichtung](../start/setup.md)). Brauchst du sie nicht mehr, lösche sie dort unter *Einstellungen → Benutzer* und *Einstellungen → API-Schlüssel*. Die Druckaufträge, die Reprintbuddy eingereiht hat, und die Druckdateien, die du darüber hochgeladen hast, bleiben in Bambuddy wie alle anderen.
