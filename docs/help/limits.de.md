# Grenzen

Was Reprintbuddy bewusst nicht tut und wo es aufhört. Das meiste davon sind Entscheidungen, keine Lücken: Reprintbuddy führt den Bestand und übergibt Aufträge an Bambuddy, und Bambuddy bleibt dein Drucksystem.

## Druckdateien müssen direkt in Bambuddys Dateimanager liegen

Reprintbuddy arbeitet zuverlässig nur mit Druckdateien, die **direkt in Bambuddys eigenem Dateimanager** liegen.

!!! warning "Dateien aus eingebundenen externen Ordnern sind nicht sicher"
    Für Dateien aus einem Ordner, der von außen in Bambuddy eingebunden ist (einem externen Ordner), führt Bambuddy keine Prüfsumme. Liest Bambuddy einen solchen Ordner erneut ein, frischt es nur das Änderungsdatum der Datei auf, nicht Platten und Filament. Reprintbuddy würde eine neue Fassung einer solchen Datei nicht bemerken.

So bemerkt Reprintbuddy eine geänderte Druckdatei: Es vergleicht die Prüfsumme, die Bambuddy für die Datei führt, mit der, die es beim Wählen der Datei gespeichert hat. Weichen sie ab oder hat die Datei einen anderen Namen, zeigt Reprintbuddy die Warnung, pausiert die Automatik für diesen Artikel und wartet, bis du die Datei bestätigst oder neu wählst (siehe [Fehlersuche](troubleshooting.md)). Für eine Datei aus einem externen Ordner gibt es keine Prüfsumme, deshalb gilt:

- Eine neue Fassung der Datei unter demselben Namen bleibt **unbemerkt**: keine Warnung, keine Pause. Der Artikel druckt weiter, mit dem, was die neue Datei enthält. Auch die Platten und das Filament, die Reprintbuddy anzeigt, können veraltet sein.
- Bemerkt wird nur noch, wenn die Datei umbenannt wird oder verschwindet.
- Reprintbuddy hält dich nicht davon ab, eine solche Datei zu wählen, warnt aber davor: Die Ordnerliste und die Plattenseite zeigen beim Wählen einen Hinweis, auch wenn du die Druckdatei eines Artikels änderst.
- In einen externen Ordner kannst du nicht hochladen: Der Knopf **Druckdatei hochladen** fehlt dort, und ein Versuch wird abgelehnt, bevor etwas an Bambuddy geht.

Am sichersten hältst du deine Druckdateien in Bambuddys eigenem Dateimanager. Nutzt ein Artikel eine Datei aus einem externen Ordner, lade die Datei in Bambuddys Dateimanager hoch oder verschiebe sie dorthin und wähle sie mit **Druckdatei ändern** für den Artikel (siehe [Artikel](../use/articles.md)).

## Was Reprintbuddy in Bambuddy nie tut

- **Es steuert nie einen Drucker.** Kein Start, Stopp, keine Pause, kein Druckerbefehl. Welcher Drucker wann druckt, entscheidet Bambuddy.
- **Es legt Einträge in Bambuddys Warteschlange an und sonst nichts.** Schaltest du **Als nächsten Druck einfügen** ein (*Einstellungen → Automatik*), setzt es den Eintrag beim Anlegen vor die Drucke, die nicht von Reprintbuddy stammen; ein laufender Druck wird nie unterbrochen. Siehe [Automatik](../use/automation.md).
- **Es ändert, sortiert, nimmt zurück oder löscht** übergebene Einträge **nie**. Was in Bambuddy liegt, gehört Bambuddy; Reihenfolge, Vorziehen und Löschen machst du dort.
- **Es ändert, ersetzt, verschiebt und löscht keine vorhandenen Dateien oder Ordner** in Bambuddy. Das Einzige, was es in Bambuddys Dateimanager schreibt, ist eine **neue Druckdatei**, die du aus Reprintbuddy hochlädst, nie unter einem schon vorhandenen Namen. Ordner legt es nicht an, und eine Datei, die es hochgeladen hat, kann es nicht löschen; das geschieht in Bambuddy. Deshalb prüft es eine Datei, bevor es etwas sendet (siehe [Artikel](../use/articles.md)).
- **Es schreibt nie in Spoolman** und spricht gar nicht mit Spoolman. Den Filamentbestand liest es über Bambuddy.

## Was Reprintbuddy nicht kennt

- **Drucke, die du direkt in Bambuddy startest.** Reprintbuddy kennt nur die Aufträge, die es selbst übergeben hat. Buche solche Stücke von Hand ein, auf der Seite *Buchen* mit **Einbuchen ohne Druck**.
- **Welche Spule Bambuddy** für einen Druck nimmt. Reprintbuddy zählt das Filament nach Spulensorte (gleiche Marke, gleiches Material, gleicher Untertyp und gleiche Farbe), nicht nach einzelner Spule. Es liest nur und ändert den Filamentbestand nie. Siehe [Filament](../use/filament.md).
- **Ob eine Push-Nachricht auf deinem Handy ankam.** „Angenommen“ heißt nur, dass Home Assistant sie genommen hat. Siehe [Push-Nachrichten](../use/notifications.md).

## Artikel und Platten

- Ein Artikel hat **genau eine Platte** einer Druckdatei. Ein Produkt aus mehreren verschiedenen Platten bildet Reprintbuddy nicht als einen Artikel ab.
- Eine Platte gehört zu **höchstens einem Artikel**. Einen zweiten Artikel auf derselben Platte lehnt Reprintbuddy ab („Diese Platte gehört schon zum Artikel …“).
- Ein Set oder ein Artikel aus Teilen hat **höchstens 100 Varianten**. Siehe [Sets](../use/inspection.md#sets) und [Artikel aus Teilen](../use/assembled.md).
- Artikelnummern werden **ohne Groß- und Kleinschreibung** verglichen. Zwei Nummern aus einer früheren Version, die sich nur darin unterscheiden, bleiben, wie sie sind, und lassen sich weiter bestellen; speichern lässt sich einer der beiden Einträge erst wieder, wenn du eine umbenennst.

## Etiketten

Etiketten werden auf einem Brother-Etikettendrucker in deinem Netz gedruckt, auf der Rolle 62 × 29 mm (DK-11209), immer quer. Siehe [Etiketten und QR-Codes](../use/labels.md).

## Konten, Sprachen, Prozesse

- **Ein Konto.** Es gibt genau einen Benutzer, den Admin. Es gibt keine Rollen und keine zweite Anmeldung.
- **Englisch und Deutsch** sind die einzigen Sprachen der Oberfläche.
- **Ein Prozess.** Reprintbuddy ist dafür gebaut, als ein Server-Prozess zu laufen; mehrere Kopien auf derselben Datenbank werden nicht unterstützt.

## Netz

- **Stelle Reprintbuddy nicht ins Internet.** Keine Portfreigabe, kein öffentlicher Reverse-Proxy. Für den Zugriff von unterwegs nimm ein VPN in dein Netz. Siehe [Netz und Sicherheit](../operate/network.md).
- **Kein Unterpfad hinter einem Reverse-Proxy.** Hinter einem Proxy läuft Reprintbuddy nur unter einer eigenen Adresse oder einem eigenen Port (`https://reprintbuddy.example.net/` oder `https://example.net:8443/`), nicht unter `https://example.net/reprintbuddy/`. Links und Weiterleitungen würden am Proxy vorbeizeigen, und Reprintbuddy schreibt eine Warnung ins Systemprotokoll, wenn es auf diesem Weg erreicht wird.
