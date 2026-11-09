# Reprintbuddy

**Dein Regal bleibt voll. Bambuddy druckt.**

Reprintbuddy führt den Lagerbestand von 3D-gedruckten Teilen. Was knapp wird, druckt es nach, indem es Druckaufträge in die Warteschlange von Bambuddy stellt. Fertige Drucke prüfst du in Reprintbuddy, und es zeigt dir, was nachzubestellen ist. Es läuft als kleine Webanwendung im lokalen Netzwerk (LAN) und ist von außen nur über ein VPN erreichbar.

!!! note "Stand"
    In Entwicklung, noch nicht einsatzbereit.

Reprintbuddy ist ein eigenständiges Projekt und nicht mit Bambuddy oder Bambu Lab verbunden.

## Was es ist und was nicht

- **Es ist** ein Lagerbuch je Artikel mit Mindestbestand und Puffer, eine eigene Warteschlange für Nachdrucke, eine Prüfliste für fertige Drucke, Buchungen für die Montage und Bestellvorschläge für Filament und Zukaufteile.
- **Es steuert nie Drucker.** Bambuddy bleibt dein Drucksystem. Reprintbuddy legt nur Aufträge in Bambuddys Warteschlange und liest den Status. Es startet, stoppt und pausiert nichts und schreibt nie in Spoolman.
- Es bringt keine eigenen Artikel mit: Artikel, Sets, Zukaufteile und Regeln legst du in der Weboberfläche an.

## So arbeitet es

1. Du legst einen **Artikel** aus einer Druckdatei an, die in Bambuddys Dateimanager liegt, und gibst ihm einen Mindestbestand.
2. Fällt der Bestand unter den Mindestbestand, stellt Reprintbuddy einen **Nachdruck** in seine eigene Warteschlange und übergibt ihn an Bambuddy, einen Druck nach dem anderen.
3. Bambuddy druckt. Ist ein Druck fertig, erscheint er unter **Zu prüfen**. Du zählst die guten Stücke und buchst sie in den Bestand.
4. Reprintbuddy beobachtet dein **Filament** und die **Zukaufteile** und schlägt vor, was nachzubestellen ist.

## Das Menü

Das Menü links (am Handy ein Knopf) führt zu jeder Seite. Am Fuß stehen **Bambuddy öffnen** (wenn Bambuddy verbunden ist) und **Abmelden**, darunter zwei Symbole: Das GitHub-Zeichen öffnet den Quellcode, das aufgeschlagene Buch diese Dokumentation, jeweils in einem neuen Tab. Darunter steht die Version; am Computer daneben der Knopf, der das Menü auf Symbole einklappt. Mit der deutschen Oberfläche führt der Link zu den deutschen Seiten.

## Wie es weitergeht

| Ich will … | Seite |
|---|---|
| sehen, was ich vor dem Start brauche | [Voraussetzungen](start/requirements.md) |
| Reprintbuddy installieren | [Installation](start/install.md) |
| es mit Bambuddy verbinden | [Ersteinrichtung](start/setup.md) |
| wissen, was es nicht tut | [Grenzen](help/limits.md) |

## Probleme und Fragen

Fehler, Wünsche und Fragen: [ein Issue auf GitHub anlegen](https://github.com/Eifel-Joe/reprintbuddy/issues). Sicherheitslücken nie als öffentliches Issue, siehe [SECURITY.md](https://github.com/Eifel-Joe/reprintbuddy/blob/main/SECURITY.md).
