# Mitmachen

[English: CONTRIBUTING.md](CONTRIBUTING.md)

Danke für deine Hilfe. Issues und Pull Requests dürfen auf Deutsch oder
Englisch geschrieben sein.

## Wohin was gehört

- **Fehler, Wünsche und Fragen:** ein Issue mit der passenden Vorlage anlegen.
  Fragen sind willkommen, schreib das in den Titel.
- **Sicherheitslücken:** nie als öffentliches Issue. Siehe
  [SECURITY.md](SECURITY.md).

## Ein guter Fehlerbericht

Bitte angeben (die Fehler-Vorlage fragt jeden Punkt ab):

- **Reprintbuddy-Version** und **Bambuddy-Version**,
- ob du **Spoolman** benutzt (ja oder nein),
- die **Schritte** zum Nachstellen,
- was du **erwartet** hast und was **tatsächlich** passiert ist,
- einen Auszug aus dem Protokoll, **ohne API-Schlüssel und Passwörter**.

## Was nicht angenommen wird

- **Druckersteuerung in Reprintbuddy.** Bambuddy druckt; Reprintbuddy reiht nur
  Aufträge ein und liest den Status.
- Eine Abhängigkeit von **Docker** oder von **Cloud-Diensten**. Reprintbuddy wird
  nativ installiert und läuft in deinem eigenen Netz.

## Pull Requests

- Bei größeren Änderungen **vorher ein Issue anlegen** und den Weg absprechen.
- **Ein Thema je Pull Request.**
- **Tests für neues Verhalten** schreiben, und **alle Tests müssen grün sein**.
- Keine Schlüssel, Passwörter oder persönlichen Daten, auch nicht in Tests oder
  Beispielen. Neutrale Beispieldaten benutzen.
- **Dokumentation in beiden Sprachen** (Englisch und Deutsch) anpassen. Die README
  und die Seiten unter `docs/` kommen im **selben Pull Request** mit, wenn sich
  Installation, Einrichtung oder Bedienung ändern; die Pull-Request-Vorlage fragt
  danach.

## Dokumentation

Die Seiten unter `docs/` werden mit MkDocs gebaut und auf GitHub Pages
veröffentlicht. Jede Seite gibt es zweimal: `seite.md` auf Englisch und daneben
`seite.de.md` auf Deutsch.

```bash
.venv/bin/pip install -e ".[docs]"
.venv/bin/mkdocs serve           # Vorschau im Browser
.venv/bin/mkdocs build --strict  # das läuft in der CI: ein kaputter Link fällt durch
```

Bildschirmfotos zeigen nur erfundene Beispieldaten, nie echte Artikel, Namen oder
Adressen. `tools/docs_screenshots.py` nimmt sie von einer Testinstanz auf
(Aufruf im Kopf des Skripts); nach einer geänderten Oberfläche läuft es erneut.

## Lizenz von Beiträgen

Wer einen Beitrag einreicht, stellt ihn unter **AGPL-3.0-or-later**, die Lizenz
dieses Projekts, und erlaubt dem Betreuer (Eifel-Joe) zusätzlich, ihn auch unter
anderen Lizenzbedingungen weiterzugeben.

## Entwicklung

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest
```

Die Tests sprechen nie mit Bambuddy; es wird mit `respx` nachgebildet.
Einstellungen kommen nicht aus Dateien oder Umgebungsvariablen: Jede Einstellung
liegt in der Datenbank und wird in der Weboberfläche geändert.

## Umgang mit Issues und Pull Requests

Pull Requests werden erst nach Freigabe durch den Betreuer gemergt.
