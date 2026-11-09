# Voraussetzungen

Was du brauchst, bevor du Reprintbuddy installierst: ein passendes Bambuddy, Druckdateien am richtigen Ort, ein System, auf dem es läuft, und ein paar Netzverbindungen.

## Bambuddy

- **Bambuddy 1.3 oder neuer.** Bambuddy kann die Spulen selbst führen (eigene Spulenverwaltung) oder in Spoolman. **Spoolman ist optional.** Reprintbuddy liest den Filamentbestand über Bambuddy und spricht nie selbst mit Spoolman.
- **Druckdateien direkt in Bambuddys Dateimanager**, hochgeladen und geslict (`.gcode.3mf`).

    !!! warning "Dateien aus eingebundenen externen Ordnern reichen nicht"
        Für Dateien aus einem Ordner, der von außen in Bambuddy eingebunden ist (einem externen Ordner), führt Bambuddy keine Prüfsumme, und beim erneuten Einlesen eines solchen Ordners liest es Platten und Filament nicht neu. Reprintbuddy würde eine neue Fassung einer solchen Datei nicht bemerken. Nimm Dateien, die direkt in Bambuddys eigenem Dateimanager liegen. Mehr unter [Grenzen](../help/limits.md).

    Neue Druckdateien kannst du auch aus Reprintbuddy hochladen (optional, siehe [Artikel](../use/articles.md)), aber nicht in einen externen Ordner.
- **Ein eigener Bambuddy-Benutzer und eine eigene Gruppe.** Reprintbuddy braucht einen Benutzer `reprintbuddy` in einer eigenen Bambuddy-Gruppe `Reprintbuddy`, mit den Rechten, die unter [Ersteinrichtung](setup.md) stehen, und einen **API-Schlüssel** dieses Benutzers. Der eigene Benutzer macht die Aufträge von Reprintbuddy in Bambuddy erkennbar. Die Einrichtung führt dich Schritt für Schritt durch.

## Das System für Reprintbuddy

- Ein Rechner, eine VM oder ein Container (z. B. Proxmox-LXC, unprivilegiert genügt) mit **Debian 13** oder neuer. Das Installationsskript lehnt andere Systeme ab.
- Ein CPU-Kern, 1 GB RAM und 8 GB Platte reichen.
- Kein Docker.

Reprintbuddy braucht Python 3.13 oder neuer. Debian 13 bringt es mit; Debian 12 hat nur Python 3.11, deshalb bricht das Installationsskript dort ab. Wie du ein älteres System auf Debian 13 bringst, ohne etwas zu verlieren, steht unter [Installation](install.md).

## Netz

- **Ausgehend:** vom Reprintbuddy-System zu den Debian-Paketquellen (`apt`), zu `github.com` und `raw.githubusercontent.com` (Installation und Updates), `api.github.com` (die Versionshinweise auf der Update-Karte), zu `pypi.org` und `files.pythonhosted.org` (Python-Pakete, bei der Installation und bei jedem Update) und zu deinem Bambuddy.
- **Eingehend:** die Browser, mit denen du arbeitest, müssen Reprintbuddy an seinem Port erreichen (Vorgabe 8080). Stelle es nicht ins Internet; für den Zugriff von unterwegs nimm ein VPN. Siehe [Netz und Sicherheit](../operate/network.md).

## Wahlweise: Home Assistant

Für Push-Nachrichten aufs Handy kann Reprintbuddy an [Home Assistant](https://www.home-assistant.io/) und dessen Handy-App melden. Dann braucht Reprintbuddy auch Zugang zu Home Assistant. Ohne Eintrag wird nichts gesendet und Home Assistant nie angesprochen. Siehe [Push-Nachrichten](../use/notifications.md).
