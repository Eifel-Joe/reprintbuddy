<p align="center">
  <img src="src/reprintbuddy/web/static/img/logo.svg" alt="Reprintbuddy" width="128" height="128">
</p>

<h1 align="center">Reprintbuddy</h1>

<p align="center">
  <strong>Dein Regal bleibt voll. Bambuddy druckt.</strong><br>
  Selbst gehostete Lagerführung für 3D-gedruckte Teile, Nachdrucke über die Warteschlange von Bambuddy.
</p>

<p align="center">
  <a href="https://github.com/Eifel-Joe/reprintbuddy/releases"><img src="https://img.shields.io/github/v/release/Eifel-Joe/reprintbuddy" alt="Release"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Eifel-Joe/reprintbuddy/ci.yml?branch=main&logo=github&label=CI" alt="CI"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/blob/main/LICENSE"><img src="https://img.shields.io/github/license/Eifel-Joe/reprintbuddy" alt="License: AGPL-3.0"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/stargazers"><img src="https://img.shields.io/github/stars/Eifel-Joe/reprintbuddy?style=flat" alt="Stars"></a>
  <a href="https://github.com/Eifel-Joe/reprintbuddy/issues"><img src="https://img.shields.io/github/issues/Eifel-Joe/reprintbuddy" alt="Issues"></a>
</p>

[English: README.md](README.md)

Reprintbuddy führt den Lagerbestand von 3D-gedruckten Teilen. Was knapp wird,
druckt es nach, indem es Druckaufträge in die Warteschlange von
Bambuddy stellt. Fertige Drucke prüfst
du in Reprintbuddy, und es zeigt dir, was nachzubestellen ist. Es läuft als
kleine Webanwendung im Heimnetz und ist von außen nur über ein VPN erreichbar.

> **Stand:** in Entwicklung, noch nicht einsatzbereit.

Reprintbuddy ist ein eigenständiges Projekt und nicht mit Bambuddy oder Bambu Lab verbunden.

## Was es ist und was nicht

- **Es ist** ein Lagerbuch je Artikel mit Mindestbestand und Puffer, eine eigene
  Warteschlange für Nachdrucke, eine Prüfliste für fertige Drucke, Buchungen
  für die Montage und Bestellvorschläge für Filament und Zukaufteile.
- **Es steuert nie Drucker.** Bambuddy bleibt dein Drucksystem. Reprintbuddy legt
  nur Aufträge in Bambuddys Warteschlange und liest den Status. Es startet,
  stoppt und pausiert nichts und schreibt nie in Spoolman.
- Es bringt keine eigenen Artikel mit: Artikel, Sets, Zukaufteile und Regeln
  legst du in der Weboberfläche an.

## Voraussetzungen

- Bambuddy 1.3 oder neuer, mit eigener Spulenverwaltung oder mit Spoolman (**Spoolman ist
  optional**). Reprintbuddy liest den Filamentbestand über Bambuddy und spricht nie selbst
  mit Spoolman.
- **Druckdateien direkt in Bambuddys Dateimanager**, hochgeladen und geslict (`.gcode.3mf`).
  Extern eingebundene Ordner reichen nicht: Für ihre Dateien führt Bambuddy keine Prüfsumme
  und liest Platten und Filament beim erneuten Einlesen nicht neu, eine geänderte Fassung
  bliebe unbemerkt.
- Ein **eigener Bambuddy-Benutzer** `reprintbuddy` in einer **eigenen Bambuddy-Gruppe** mit den
  Rechten, die unter *Erster Aufruf* stehen, und ein **API-Schlüssel** dieses Benutzers. Der
  eigene Benutzer macht Reprintbuddys Aufträge in Bambuddy erkennbar. Die Einrichtung
  führt dich Schritt für Schritt durch.
- Ein **Proxmox-LXC-Container** (unprivilegiert genügt) oder ein anderer Rechner mit
  **Debian 12 oder 13**. Ein CPU-Kern, 1 GB RAM und 8 GB Platte reichen. Docker ist
  nicht nötig.
- Ausgehender Netzzugang dieses Rechners zu den Debian-Paketquellen (`apt`),
  `github.com` und `raw.githubusercontent.com` (Installation und Updates),
  `pypi.org` und `files.pythonhosted.org` (Python-Pakete, bei der Installation und
  bei jedem Update) und zu deinem Bambuddy.

## Installation

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v0.3.6/install/install.sh)"
```

Als root im Container ausführen. Frische Debian-Container bringen oft kein curl mit; der erste Teil installiert es.
Der Tag in der Adresse (`v0.3.6`) ist die Version, die installiert wird; Skript und Code gehören sicher zusammen.
Ohne Rückfrage nach der Sprache, mit angehängter Sprache:

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v0.3.6/install/install.sh)" _ --language de
```

Für eine andere Version setze ihren Tag in die Adresse (für `<tag>` zum Beispiel `v0.3.6`):

```bash
apt-get update && apt-get install -y curl && bash -c "$(curl -fsSL https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/<tag>/install/install.sh)"
```

Das Skript

- installiert die nötigen Pakete (`python3`, `python3-venv`, `git`, `curl`, `sudo`,
  `ca-certificates`),
- legt den Systembenutzer `reprintbuddy` an,
- legt Reprintbuddy auf dem neuesten Release-Tag nach `/opt/reprintbuddy` und die
  Python-Umgebung nach `/opt/reprintbuddy/.venv`,
- legt `/var/lib/reprintbuddy` (Rechte 700) mit Datenbank, Sicherungen und Update-Stand an,
- richtet die systemd-Einheiten `reprintbuddy.service` und `reprintbuddy-update.service` ein,
- installiert eine sudoers-Regel in `/etc/sudoers.d/reprintbuddy`, die dem Dienstbenutzer genau
  genau diese Befehle als root erlaubt und nichts sonst (die letzten beiden braucht der Rückfall eines
  gescheiterten Updates, der die Datenbank bei gestopptem Dienst zurückspielt):

  ```text
  /usr/bin/systemctl restart --no-block reprintbuddy.service
  /usr/bin/systemctl restart reprintbuddy.service
  /usr/bin/systemctl start --no-block reprintbuddy-update.service
  /usr/bin/systemctl stop reprintbuddy.service
  /usr/bin/systemctl start reprintbuddy.service
  ```

- installiert den Befehl `/usr/local/bin/reprintbuddy` (siehe *Kommandozeile*),
- startet den Dienst und nennt die Adresse für die Ersteinrichtung.

**Schließe die Einrichtung sofort ab** unter `http://<container-ip>:8080/setup`: Bis das
Admin-Konto existiert, kann es jeder in deinem Netz anlegen.

Das Skript erneut auszuführen ist sicher: Version und Datenbank bleiben, es repariert Pakete,
Benutzer, Rechte, Einheiten und den Dienst. Die Version wechselt nur das Update (siehe
*Update*). Mit `--dry-run` zeigt das Skript, was es täte, ohne etwas zu ändern.

Für die Entwicklung:

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m reprintbuddy serve --db ./dev.db
```

## Erster Aufruf

Öffne die Adresse, die das Installationsskript nennt. Solange die Einrichtung
nicht abgeschlossen ist, kann jeder in deinem Netz die Seite benutzen, also
erledige sie gleich. Bei der Ersteinrichtung öffne Reprintbuddy über seine IP-Adresse (oder einen
lokalen Namen); Namen aus öffentlichen Domains, z. B. VPN-DNS-Namen, werden erst nach der
Einrichtung akzeptiert.

1. **Admin-Konto:** Benutzername, Passwort und Sprache wählen.
2. **Gruppe und Benutzer in Bambuddy:** Lege in Bambuddy unter *Einstellungen → Benutzer* eine
   eigene Gruppe `Reprintbuddy` an. Keine Systemgruppe passt: *Operators* sieht nur eigene
   Dateien und Aufträge, *Administrators* darf alles. Gib der Gruppe diese Rechte (in Klammern
   der interne Name). Bambuddy zeigt die Rechte nur auf Englisch, deshalb stehen sie hier wie dort:

   - Read All Library (`library:read_all`): damit Reprintbuddy deine Druckdateien sieht
   - Read All Queue (`queue:read_all`): damit Reprintbuddy die ganze Warteschlange sieht
   - Create Queue (`queue:create`): damit Reprintbuddy Nachdrucke einreihen kann
   - Print Without Review (`queue:start_unreviewed`): damit Nachdrucke ohne Freigabe starten
   - Read Printers (`printers:read`): damit Reprintbuddy die Drucker und ihren Zustand sieht
   - Read Inventory (`inventory:read`): damit Reprintbuddy den Filamentbestand liest
   - Read Filaments (`filaments:read`): um zu erkennen, ob Bambuddy die Spulen selbst führt oder
     in Spoolman

   Nur zum Erzeugen des Schlüssels im nächsten Schritt braucht die Gruppe zusätzlich diese drei
   Rechte; Reprintbuddy prüft sie nicht, und sie dürfen danach wieder weg: Read Api Keys
   (`api_keys:read`), Create Api Keys (`api_keys:create`) und Read Settings (`settings:read`;
   ohne es blendet Bambuddy das Menü Einstellungen aus).

   Lege dann den Benutzer `reprintbuddy` in dieser Gruppe an. Sein Passwort brauchst du nur
   einmal, um den Schlüssel zu erzeugen; Reprintbuddy fragt es nicht ab und speichert es nicht.
3. **API-Schlüssel:** Melde dich in Bambuddy ab und als `reprintbuddy` an (ein Schlüssel gehört
   immer dem angemeldeten Benutzer). Öffne *Einstellungen → API-Schlüssel → Schlüssel erstellen*
   und stelle ihn genau so ein: Name „Reprintbuddy“; *Status lesen* (*Read Status*) **an**;
   *Warteschlange verwalten* (*Manage Queue*) **an**; alle anderen **aus** (*Control Printer*,
   *Manage Library*, *Manage Inventory*, *Manage Maintenance*, *Manage Archives*,
   *Manage Projects*, *Allow Cloud Access*, *Update Electricity Price*, *Send Notifications*).
   Kopiere den Schlüssel
   sofort (Bambuddy zeigt ihn nur einmal) und melde dich wieder ab.
4. **Bambuddy anbinden:** Trage die Adresse von Bambuddy und den Schlüssel ein. Reprintbuddy
   fragt Bambuddy (`GET /api/v1/auth/me`), wem der Schlüssel gehört und welche Rechte er wirklich
   hat, und zeigt Besitzer und jedes nötige Recht mit Haken oder Kreuz. Die Oberfläche öffnet
   sich erst, wenn kein Recht fehlt. Ein Schlüssel eines Administrators wird angenommen, mit der
   Warnung, dass er mehr darf als nötig. Dieselbe Prüfung gibt es unter *Einstellungen*.

   Die beiden Felder sind so gekennzeichnet, dass Passwort-Manager sie nicht mit deinen eigenen
   Zugangsdaten füllen.

## Einstellungen

Es gibt **keine Konfigurationsdatei**. Jede Einstellung liegt in der Datenbank
und wird in der Weboberfläche unter *Einstellungen* geändert: Sprache,
Bambuddy-Verbindung, Passwort, Adresse, Port, optionales HTTPS-Zertifikat, Neustart,
Update und Export.
Nur der Pfad der Datenbank wird auf der Kommandozeile angegeben (`--db`).

## Update

*Einstellungen → Update*: **Nach Updates suchen** drücken; gibt es eine neuere Version,
**Auf … aktualisieren**. *Verfügbar ab der ersten Version.* Das Update

1. sichert die Datenbank,
2. wechselt auf den neuen Release-Tag,
3. installiert die Pakete,
4. migriert die Datenbank,
5. startet den Dienst neu und
6. prüft, ob die neue Version antwortet.

Scheitert ein Schritt, geht Reprintbuddy auf die vorige Version zurück **und** spielt die
Datenbank aus der Sicherung zurück; die Seite nennt den gescheiterten Schritt. Änderungen aus
der Zeit des Updates gingen dabei verloren, darum sind alle Änderungen gesperrt, bis es fertig ist.

Zeigt die Seite **Update wurde unterbrochen** oder **Update fehlgeschlagen, vorige Version ließ sich nicht wiederherstellen**,
führe das Installationsskript erneut aus: Es repariert Pakete, Einheiten und den Dienst und lässt die Datenbank
stehen. Startet Reprintbuddy danach nicht, spiele die auf der Seite genannte Sicherung zurück (siehe
*Sicherung und Export*) und führe das Skript noch einmal aus. Die Seite zeigt den Git-Tag und, wenn sie sich
unterscheidet, daneben die Paketversion.

Die Sicherungen liegen in `/var/lib/reprintbuddy/backups/` (die letzten 5; eine von einem gescheiterten Rückfall
genannte Sicherung wird nie gelöscht). Ein Update von Hand
startest du mit `systemctl start reprintbuddy-update.service`; das Protokoll steht in
`journalctl -u reprintbuddy-update`.

Versionen sind Git-Tags `vX.Y.Z`, die Paketversion entspricht dem Tag. Ändert eine Version die
systemd-Einheiten oder die sudoers-Regel, steht es in der Release-Notiz; dann das
Installationsskript erneut ausführen.

## Neustart

*Einstellungen → Neustart* startet Reprintbuddy neu. Adresse, Port und HTTPS wirken erst nach
einem Neustart; die Einstellungsseite sagt, wenn eine gespeicherte Änderung noch wartet. Nach
einer Änderung von Adresse oder Port nennt die Neustart-Seite die neue Adresse. Im Container:
`systemctl restart reprintbuddy`.

## HTTPS

HTTP genügt im Heimnetz oder über ein VPN. Für HTTPS lädst du unter *Einstellungen* ein Zertifikat und seinen
privaten Schlüssel (ohne Passwort) hoch; Reprintbuddy prüft vor dem Speichern, ob beide
zusammengehören, danach neu starten. Zurück zu HTTP geht über das Häkchen *Zertifikat und
privaten Schlüssel entfernen*.

Alternativ läuft Reprintbuddy hinter einem vorhandenen Reverse-Proxy: Reprintbuddy bleibt bei
HTTP, der Proxy macht TLS. Läuft der Proxy im selben Container, stelle die Lauschadresse auf
`127.0.0.1`.

Ausgesperrt durch ein Zertifikat, das nicht mehr taugt (abgelaufen, falscher Name)? Im Container:

```bash
reprintbuddy disable-https && systemctl restart reprintbuddy
```

Bei falschem Port: `reprintbuddy set-port 8080 && systemctl restart reprintbuddy`.

## Sicherung und Export

Reprintbuddy bringt eine eigene vollständige Sicherung mit, unabhängig davon, wie der Rechner oder
Container sonst gesichert wird. Unter *Einstellungen → Update und Sicherung*:

- **Vollständige Sicherung** lädt die ganze Datenbank als eine `.zip`-Datei herunter: alle Daten
  und Einstellungen, **auch** den Bambuddy-Schlüssel, den Passwort-Hash, den privaten
  HTTPS-Schlüssel und das Sitzungsgeheimnis. Mit Passwort (8 bis 1024 Zeichen) wird sie
  verschlüsselt (Argon2id und AES-256-GCM); das Passwort wird nirgends gespeichert, ohne es lässt
  sich die Sicherung nicht wiederherstellen. **Ohne Passwort kommt jeder, der die Datei hat, an die
  Schlüssel und kann sich anmelden.** Vor dem Herunterladen fragt Reprintbuddy das Anmelde-Passwort
  erneut ab; ein offener Browser allein reicht also nicht.
- **Wiederherstellen** nimmt so eine Datei (höchstens 128 MB). Reprintbuddy prüft, dass sie eine
  Sicherung von Reprintbuddy in dieser oder einer älteren Version ist (eine ältere Datenbank wird
  beim Start migriert, eine neuere abgelehnt), fragt nach dem Anmelde-Passwort und bei Verschlüsselung
  nach dem Passwort der Sicherung, sichert
  die laufende Datenbank wie vor einem Update, ersetzt sie und startet neu. Danach gelten
  Benutzername und Passwort aus der Sicherung, und alle melden sich neu an. Auch Adresse, Port und
  HTTPS kommen aus der Sicherung; passen sie auf einem neuen Rechner nicht, richten
  `reprintbuddy set-host` und `set-port` (siehe *Kommandozeile*) sie wieder.
- **Export** lädt alle Daten als JSON-Datei herunter, **ohne** API-Schlüssel, Passwörter, den
  privaten HTTPS-Schlüssel und das Sitzungsgeheimnis.

Eine Datenbanksicherung von Hand zurückspielen:

```bash
systemctl stop reprintbuddy
runuser -u reprintbuddy -- cp /var/lib/reprintbuddy/backups/<sicherung>.db /var/lib/reprintbuddy/reprintbuddy.db
rm -f /var/lib/reprintbuddy/reprintbuddy.db-wal /var/lib/reprintbuddy/reprintbuddy.db-shm
systemctl start reprintbuddy
```

## Sicherheit

- Reprintbuddy nicht direkt ins Internet stellen (keine Portweiterleitung, kein öffentlicher
  Reverse-Proxy). Für den Zugriff von unterwegs ein VPN in dein Netz nutzen (z. B. das VPN deines
  Routers, WireGuard oder Tailscale).
- API-Schlüssel, der private Schlüssel eines HTTPS-Zertifikats und Passwörter
  sind nur beim Eintippen sichtbar. Gespeichert zeigt die Oberfläche `********`;
  ein leeres Feld behält den gespeicherten Wert, eine neue Eingabe ersetzt ihn.
  Passwörter werden nur als Hash gespeichert.
- Jede Änderung braucht eine angemeldete Sitzung und ein CSRF-Token.
- Die Datenbankdatei ist nur für den Dienstbenutzer lesbar. Wer auf dem Rechner
  Root ist, kann sie lesen.

Sicherheitslücken meldest du wie in [SECURITY.md](SECURITY.md) beschrieben.

## Kommandozeile

Im Container läuft der Befehl `reprintbuddy` als Dienstbenutzer `reprintbuddy` (auch als root),
damit die Datenbankdateien ihren Besitzer behalten:

```bash
reprintbuddy set-password --username <name>
reprintbuddy set-port <port>
reprintbuddy set-host <adresse>
reprintbuddy disable-https
reprintbuddy --version
```

## Passwort vergessen oder falscher Port

Auf dem Rechner, auf dem Reprintbuddy läuft:

```bash
reprintbuddy set-password --username admin
reprintbuddy set-port 8080
reprintbuddy set-host 0.0.0.0
```

`set-password` benennt das Konto auch um, und zwar in den angegebenen `--username`;
ein vergessener Benutzername ist also kein Hindernis.

Danach den Dienst neu starten (`set-port` und `set-host` wirken erst dann).
`set-host` setzt die Lauschadresse (zum Beispiel `0.0.0.0`), falls dich eine falsche ausgesperrt hat.

## Mitmachen

Siehe [CONTRIBUTING.de.md](CONTRIBUTING.de.md).

## Lizenz

Reprintbuddy steht unter der GNU Affero General Public License, Version 3 oder
später (AGPL-3.0-or-later). Siehe [LICENSE](LICENSE).

Copyright (C) 2026 Eifel-Joe

Bestandteile Dritter mit eigener Lizenz: die Symbole stammen von
[Lucide](https://lucide.dev) (ISC, Lizenztext in `src/reprintbuddy/web/static/icons/`);
die QR-Codes zeichnet [segno](https://github.com/heuer/segno) (BSD-3-Clause), das als
Abhängigkeit von PyPI installiert wird und nicht Teil dieses Repositories ist.
