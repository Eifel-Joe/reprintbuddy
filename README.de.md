# Reprintbuddy

[English: README.md](README.md)

Reprintbuddy führt den Lagerbestand von 3D-gedruckten Teilen. Was knapp wird,
druckt es nach, indem es Druckaufträge in die Warteschlange von
Bambuddy stellt. Fertige Drucke prüfst
du in Reprintbuddy, und es zeigt dir, was nachzubestellen ist. Es läuft als
kleine Webanwendung im Heimnetz.

> **Stand:** in Entwicklung, noch keine Version veröffentlicht.

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

- Bambuddy 1.3 oder neuer im **Spoolman-Modus** (Reprintbuddy liest den
  Filamentbestand über Bambuddy).
- Ein **eigener Bambuddy-Benutzer** für Reprintbuddy (zum Beispiel `reprintbuddy`) mit
  dem Recht **„ohne Prüfung drucken“** (*Print without review*), damit eingereihte
  Drucke ohne Bestätigung starten, und ein **API-Schlüssel** dieses Benutzers
  mit den Rechten **„Status lesen“** (*Read Status*) und **„Warteschlange
  verwalten“** (*Manage Queue*). Der eigene Benutzer macht Reprintbuddys Aufträge
  in Bambuddy erkennbar.
- Ein Linux-Rechner oder -Container mit Python 3.11 oder neuer. Docker ist nicht
  nötig.

## Installation

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/<owner>/reprintbuddy/main/install/install.sh)"
```

Das Installationsskript richtet eine Python-Umgebung und einen systemd-Dienst
ein, fragt die Sprache der Oberfläche ab (Vorgabe Englisch) und nennt die
Adresse für die Ersteinrichtung. *Verfügbar ab der ersten Version.*

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
erledige sie gleich.

1. **Admin-Konto:** Benutzername, Passwort und Sprache wählen.
2. **Bambuddy anbinden:** Adresse, Benutzername und API-Schlüssel von Bambuddy
   eintragen und bestätigen, dass der Benutzer das Recht *ohne Prüfung drucken*
   hat. Reprintbuddy prüft die Verbindung, bevor die Oberfläche freigegeben wird.

## Einstellungen

Es gibt **keine Konfigurationsdatei**. Jede Einstellung liegt in der Datenbank
und wird in der Weboberfläche unter *Einstellungen* geändert: Sprache,
Bambuddy-Verbindung, Passwort, Adresse, Port und optionales HTTPS-Zertifikat.
Nur der Pfad der Datenbank wird auf der Kommandozeile angegeben (`--db`).

## Update

*Einstellungen → Update* zeigt die installierte und die neueste freigegebene
Version und aktualisiert auf Klick, nachdem die Datenbank gesichert wurde.
*Verfügbar ab der ersten Version.*

## Sicherheit

- Reprintbuddy ist **nur für dein Heimnetz** gedacht. Nicht aus dem Internet
  erreichbar machen.
- API-Schlüssel, der private Schlüssel eines HTTPS-Zertifikats und Passwörter
  sind nur beim Eintippen sichtbar. Gespeichert zeigt die Oberfläche `********`;
  ein leeres Feld behält den gespeicherten Wert, eine neue Eingabe ersetzt ihn.
  Passwörter werden nur als Hash gespeichert.
- Jede Änderung braucht eine angemeldete Sitzung und ein CSRF-Token.
- Die Datenbankdatei ist nur für den Dienstbenutzer lesbar. Wer auf dem Rechner
  Root ist, kann sie lesen.

Sicherheitslücken meldest du wie in [SECURITY.md](SECURITY.md) beschrieben.

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
