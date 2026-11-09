# Übersicht

Die **Übersicht** ist die Startseite. Sie zeigt auf einen Blick, ob Reprintbuddy arbeiten kann, was auf dich wartet und wie voll jeder Artikel ist. Wie jede Seite bleibt sie von selbst aktuell: Eine Buchung auf einem anderen Gerät, ein fertiger Druck in Bambuddy oder die Automatik zeigt sich hier nach wenigen Sekunden.

![Die Übersicht mit Kopfzeile, Warnungen, den vier Kennzahlen und der Tabelle der Artikel](../assets/images/de/overview.png)

Von oben nach unten:

## Kopfzeile

| Teil | Bedeutung |
|---|---|
| **Bambuddy** | Der Zustand der Verbindung: **verbunden**, **nicht erreichbar** oder **Antwort abgelehnt** (rot), **nicht eingerichtet** oder **noch nicht abgefragt** (grau: Reprintbuddy hat Bambuddy seit dem Start noch nicht erreicht). |
| **Letzte Abfrage** | Wann Bambuddy zuletzt auf eine reguläre Abfrage geantwortet hat, oder **noch keine**. |
| **Automatik** | Der Hauptschalter der Automatik (siehe [Automatik](automation.md)). Es ist derselbe Schalter wie unter *Einstellungen → Automatik*; hier wirkt er sofort, ohne Speichern-Knopf. Aus: Die Regel legt keine Einträge in der Warteschlange an; Ersatz und von Hand drucken gehen weiter. |

**Jetzt bei Bambuddy abfragen** oben rechts fragt Bambuddy sofort, statt auf die nächste reguläre Abfrage zu warten.

## Warnungen

Unter der Kopfzeile stehen die Warnungen und die pausierten Artikel, jeweils als Karte mit dem Weg weiter (zum Beispiel **Pause aufheben**). Rot heißt: Bambuddy antwortet nicht oder lehnt ab, eine Übergabe ist unklar oder eine Push-Nachricht ging nicht durch; gelb ist ein Zustand, den du gewählt hast oder der auf dich wartet. Die Seite [Fehlersuche](../help/troubleshooting.md) nennt jede Warnung und was zu tun ist. Ohne Warnungen bleibt dieser Teil leer.

## Kennzahlen

Vier Karten, immer alle vier, auch mit 0. Jede Karte führt zu ihrer Seite.

| Karte | Zählt | Führt zu |
|---|---|---|
| **Unter Mindestbestand** | Artikel mit dem Status **unter Minimum** oder **überbucht** (rot). Ein Artikel, der **knapp** ist (gelb), zählt nicht: Sein Nachdruck ist schon geplant. Die kleine Zeile nennt, wie viele davon überbucht sind. | Artikel |
| **Geplant und in Arbeit** | Drucke in Reprintbuddys Warteschlange, auch zurückgehaltene und Einträge pausierter Artikel (sie zählen für die Nachdruck-Regel weiter mit), plus Drucke, die an Bambuddy übergeben und noch nicht fertig sind. Die kleine Zeile trennt beides. Die Karte **Wartende Drucke** auf der Warteschlange lässt zurückgehaltene und pausierte Einträge weg und kann deshalb weniger zeigen. | Warteschlange |
| **Zu prüfen** | Stück, die auf die Prüfung warten, plus Stück, die auf die Montage warten. Die kleine Zeile trennt beides. | Zu prüfen; Montage, wenn nur die Montage wartet |
| **Bestellvorschläge** | Die offenen Vorschläge für Filament und Zukaufteile, dieselbe Zahl wie auf der Seite [Bestellvorschläge](reorder.md). | Bestellvorschläge |

## Artikel

Eine Tabelle aller aktiven Artikel, nach Namen geordnet wie die Artikelliste. Archivierte Artikel fehlen.

| Spalte | Bedeutung |
|---|---|
| **Name / Artikelnummer** | Führt zur Seite des Artikels. |
| **Verfügbar** | Bestand minus reserviert. Rot unter 0 (mehr bestellt als da). |
| **Reserviert** | Stück offener Bestellungen. |
| **Min / Ziel** | Mindestbestand und Zielmenge (Mindestbestand + Puffer). |
| **In Arbeit** | Stück der Drucke, die an Bambuddy übergeben sind. |
| **Zu prüfen** | Stück, die auf die Prüfung oder die Montage warten. |
| **Füllstand** | Verfügbar im Verhältnis zur Zielmenge, gefärbt wie der Status. |
| **Status** | **überbucht**, **unter Minimum**, **knapp** oder **reicht**, dazu **pausiert**, wenn die Automatik für den Artikel pausiert. **Nachdruck läuft (n)** nennt die Drucke des Artikels in Bambuddy. |

Die Status folgen überall in Reprintbuddy denselben Regeln, siehe [Die Artikelliste](articles.md#die-artikelliste).

Am Handy ist die Seite einspaltig: Die Kopfzeile wird zu Zeilen aus Bezeichnung und Wert, je zwei Kennzahlen stehen nebeneinander, und jeder Artikel wird eine Karte mit Name und Status oben und den Zahlen darunter.

## Eine frische Installation

Gleich nach der Ersteinrichtung gibt es noch keine Artikel. Die Kennzahlen zeigen 0, und statt der Tabelle sagt die Seite das und bietet **Neuer Artikel aus Druckdatei** an. Bis Reprintbuddy Bambuddy erreicht hat, steht in der Kopfzeile **noch nicht abgefragt**.
