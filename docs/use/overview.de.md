# Übersicht

Die **Übersicht** ist die Startseite. Sie zeigt auf einen Blick, ob Reprintbuddy arbeiten kann und was ansteht: keine Listen, nur Zahlen, und jede führt zu der Seite, auf der du es erledigst. Wie jede Seite bleibt sie von selbst aktuell: Eine Buchung auf einem anderen Gerät, ein fertiger Druck in Bambuddy oder die Automatik zeigt sich hier nach wenigen Sekunden.

![Die Übersicht mit Kopfzeile, Warnungen und den Karten zu dem, was ansteht](../assets/images/de/overview.png)

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

## Was ansteht

Sieben Karten, immer alle sieben. Eine Karte, bei der nichts zu tun ist, bleibt grau mit 0; gelb heißt, es steht etwas an, rot gilt für Artikel unter ihrem Mindestbestand. Die ganze Karte führt zu ihrer Seite; ein unterstrichener Teil der kleinen Zeile führt zu einer gefilterten Liste.

| Karte | Zahl | Kleine Zeile | Führt zu |
|---|---|---|---|
| **Bestellungen** | Offene Bestellungen, versandbereite mitgezählt. | **n neu** · **n versandbereit** (führt zu den versandbereiten offenen Bestellungen) | Bestellungen |
| **Zu prüfen** | Fertige Drucke, die auf die Prüfung warten. | **n Stück** in diesen Drucken | Zu prüfen |
| **Montage** | Stück, die nach der Prüfung auf die Montage warten, plus Stück offener Bestellungen von Artikeln aus Teilen, die erst bei Bestellung montiert werden. Varianten auf Vorrat unter ihrer Zielmenge zählen nicht: Den Vorrat aufzufüllen steht nicht an. | – | Montage |
| **Bestellvorschläge** | Vorschläge für Filament und Zukaufteile, die noch nicht als bestellt markiert sind, dieselbe Zahl wie auf [Bestellvorschläge](reorder.md). | **n neu** · **n unterwegs** (als bestellt markiert, noch nicht eingetroffen) | Bestellvorschläge |
| **Drucke** | Drucke, die Bambuddy laufen lässt. Ein Druck zählt als laufend, sobald Bambuddy dem Drucker den Startbefehl schickt; Aufheizen und Leveln zählen also mit. | **n in der Warteschlange**: Reprintbuddys Warteschlange (auch zurückgehaltene Einträge und Einträge pausierter Artikel) plus Drucke, die an Bambuddy übergeben sind und dort auf ihren Start warten | Warteschlange |
| **Filament** | Spulensorten, die **knapp** sind: Sie haben einen Bestellvorschlag. Eine Sorte, die als bestellt markiert ist, ist unterwegs und nicht mehr knapp. Solange der Spulenbestand unbekannt ist (Bambuddy nicht erreichbar und nichts aufgezeichnet), steht auf der Karte **Bestand unbekannt**. | – | Filament |
| **Unter Mindestbestand** | Artikel mit dem Status **unter Minimum** oder **überbucht** (rot); jede Variante eines Artikels aus Teilen auf Vorrat zählt für sich. Ein Artikel, der **knapp** ist (gelb), zählt nicht: Sein Nachdruck ist schon geplant. | **n überbucht** (führt zu den überbuchten Artikeln) | Artikel, gefiltert auf **unter Minimum**. Die Liste zeigt eine Zeile je Artikel und die überbuchten unter ihrem eigenen Filter; sie kann deshalb weniger zeigen, als die Karte zählt. |

### Neu

**Neu** heißt: seit du die Seite **Bestellungen** bzw. **Bestellvorschläge** zuletzt geöffnet hast, egal auf welchem Gerät. Es gibt ein Konto; wer am Handy die Bestellungen öffnet, löscht „neu“ auch am PC. Eine Bestellung ist neu, wenn sie danach angelegt wurde; ein Vorschlag, wenn die Seite ihn damals nicht gezeigt hat. Der Blick auf die Übersicht löscht nichts, und eine Seite, die nur offen bleibt und sich selbst aktualisiert, auch nicht, ebenso wenig eine Seite, die Reprintbuddy von selbst neu lädt (nach einem Update oder Neustart, wenn das Nachladen eines Teils nicht klappte, oder nach einer Abmeldung): Es zählt nur, wenn du die Seite selbst öffnest, per Klick, Adresse oder Neuladen von Hand. Solange eine Seite noch nie geöffnet wurde, ist nichts neu.

Am Handy ist die Seite einspaltig: Die Kopfzeile wird zu Zeilen aus Bezeichnung und Wert, und die Karten stehen untereinander.

## Eine frische Installation

Gleich nach der Ersteinrichtung gibt es noch keine Artikel. Die Karten zeigen 0, und darunter sagt die Seite das und bietet **Neuer Artikel aus Druckdatei** an. Bis Reprintbuddy Bambuddy erreicht hat, steht in der Kopfzeile **noch nicht abgefragt**.
