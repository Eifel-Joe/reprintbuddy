# Filament

Reprintbuddy beobachtet dein Filament, damit es rechtzeitig Bestellungen vorschlagen kann. Bambuddy führt das Filament; Reprintbuddy liest es nur. Diese Seite erklärt, was Spulensorten sind, wie du einem Artikel sagst, welches Filament er nutzen darf, und die Seite *Filament* mit den Bestelleinstellungen.

## Woher die Filamentdaten kommen

- Bambuddy führt das Filament, entweder mit seiner eigenen Spulenverwaltung oder im Spoolman-Modus. **Spoolman ist optional.** Reprintbuddy liest die Spulenliste über Bambuddy, aus der Betriebsart, die gerade läuft, und schreibt nie hinein. Mit Spoolman selbst spricht es nie. Dafür braucht die Bambuddy-Gruppe die Rechte aus der [Ersteinrichtung](../start/setup.md).
- Bambuddy gibt Spulen keine Kennung, die sie zusammenfasst. Eine **Spulensorte** sind deshalb alle Spulen mit gleicher Marke, gleichem Material, gleichem Untertyp und gleichem Farbwert. Der Farbname zählt nicht. Sorten werden nie zusammengelegt: zwei Spulen, deren Farbwerte sich leicht unterscheiden, sind zwei Sorten (du kannst für einen Artikel beide ankreuzen).
- Der **Bestand** einer Spulensorte ist die Summe der Restgewichte ihrer Spulen, die nicht archiviert sind, in Gramm.
- Bei jeder regelmäßigen Abfrage hält Reprintbuddy das Restgewicht jeder Spule für den Tag fest. Aus den letzten 30 Tagen errechnet es den **Verbrauch je Tag** einer Sorte. Nur Abnahmen zählen; eine neue oder nachgefüllte Spule bringt nichts. Tage sind Kalendertage in der Zeitzone des Servers; ein neuer Tag beginnt dort um Mitternacht.
- Archivierst du eine Spule, zählt ihr Verbrauch bis zu ihrem letzten Restgewicht weiter, auch am Tag des Archivierens: Eine Spule, die leer gedruckt und gleich archiviert wird, verliert ihren letzten Verbrauch nicht. Im Bestand und bei den Spulen zählt sie ab dann nicht mehr. Wird eine Spule in Bambuddy gelöscht statt archiviert, fällt ihr Verbrauch des laufenden Tages weg.

## Einem Artikel sagen, welches Filament er nutzen darf

Ein Artikel nennt nicht eine einzelne Spule. Für jeden Filament-Slot seiner Druckdatei kreuzt du die Spulensorten an, die der Artikel nutzen darf. Zusammen sind sie die **Filamentgruppe** des Slots. Eine davon ist **Nachgekauft**: die Sorte, die wieder gekauft wird und für die Reprintbuddy Bestellungen vorschlägt.

![Die Filamentzuordnung eines Artikels: je Slot die anzukreuzenden Spulensorten, ihr Bestand und die Wahl der nachgekauften](../assets/images/de/article-filaments.png)

Reprintbuddy öffnet die Zuordnung gleich nach dem Anlegen eines Artikels und nach **Druckdatei ändern**, wenn Slots neu angekreuzt werden müssen. Du kannst sie jederzeit von der Artikelseite aus mit **Spulensorten ankreuzen** öffnen.

1. Kreuze für jeden Slot (er steht als „Slot 1 · PLA · 12,5 g“ mit der Farbe der Datei da) in der Spalte **Nutzen** die Spulensorten dieses Artikels an. Jede Zeile zeigt Marke, Untertyp, Farbe und den aktuellen Bestand in Gramm.
2. Kreuzt du eine Sorte an, ist sie von selbst die nachgekaufte. Kreuzt du mehrere an, wähle den Knopf in der Spalte **Nachgekauft**.
3. Klicke auf **Speichern**. **Später** (oder **Abbrechen**, wenn du nicht vom Anlegen des Artikels kommst) verlässt die Seite ohne Speichern.

Welche Sorten zur Wahl stehen:

- Nur Sorten, die zum Slot passen: gleiches Material und ähnliche Farbe, mit einem Farbabstand (CIEDE2000) von höchstens ΔE 7. Eine transparente Farbe passt nur zu transparent. Marke und Untertyp müssen nicht übereinstimmen. Ein Slot ohne Material oder Farbe in der Datei bietet nichts an. Passt nichts, steht das beim Slot.
- Eine Sorte, die du früher angekreuzt hast, bleibt in der Liste, auch wenn sie nicht mehr passt.
- Vorausgewählt ist nichts, mit einer Ausnahme: bei einem **neuen** Artikel beginnt die Seite mit der Gruppe eines ähnlichen Artikels. Jeder Slot eines anderen aktiven Artikels mit gleichem Material und gleicher Farbe zählt; passen mehrere Artikel, gewinnt der zuletzt angelegte. Die Seite sagt „Vorschlag aus dem ähnlichen Artikel …“, und der Vorschlag gilt erst mit **Speichern**. Bis dahin gilt der Slot als nicht zugeordnet.

Ein Slot ohne angekreuzte Sorten zählt weder für Bestand noch für Bedarf, und der Artikel zeigt **Filament nicht zugeordnet**. Auch die Seite *Filament* listet diese Slots, mit einem Link zur Zuordnung. Das Drucken wird dadurch nicht verhindert.

Die Zuordnung steuert nur Reprintbuddys eigene Rechnung. Welche Spule Bambuddy beim Drucken nimmt, entscheidet Bambuddy. Der Bestand der nachgekauften Sorte ist die Summe aller Sorten, die in den Slots angekreuzt sind, in denen sie nachgekauft wird.

Die Zuordnung eines archivierten Artikels lässt sich nur ansehen. Nach **Druckdatei ändern** behält ein Slot seine Gruppe nur bei gleichem Material und gleicher Farbe, siehe [Artikel](articles.md#druckdatei-andern).

## Die Seite Filament

*Filament* listet die Spulensorten, die in mindestens einem aktiven Artikel angekreuzt sind.

![Die Seite Filament mit den Spulensorten, ihrem Bestand, dem Verbrauch mit der Wahl „Spalte Verbrauch“ darüber und dem geplanten Bedarf](../assets/images/de/filament-page.png)

| Spalte | Bedeutung |
|---|---|
| **Spulensorte** | Marke, Material, Untertyp und Farbe. Die Plakette sagt, ob die Sorte nachgekauft wird und ob ihre Einstellungen dann **eingestellt** oder **nicht eingestellt** sind; sonst **nicht nachgekauft**. Ein gelbes **knapp** steht daneben, solange die Sorte einen Bestellvorschlag hat (siehe [Bestellvorschläge](reorder.md)); die Kachel **Filament** der [Übersicht](overview.md) zählt diese Sorten und nennt, wie viele Tage die nachgekauften Sorten reichen. Verbrauch je Tag und Reichweite beruhen auf den letzten 30 Tagen und werden erst nach einiger Zeit Betrieb zuverlässig. |
| **Material** | Das Material der Sorte. |
| **Bestand** | Summe der Restgewichte in Gramm. |
| **Spulen** | Die Zahl der Spulen dieser Sorte. |
| **Verbrauch** | Umschaltbar, siehe unten. Vorgabe: Gramm je Tag aus den letzten 30 Tagen (**Ø 30 Tage**), oder „noch kein Verlauf“, bis zwei verschiedene Tage festgehalten sind. |
| **Geplanter Bedarf** | Nur bei nachgekauften Sorten: die Gramm für die Drucke in Reprintbuddys Warteschlange und in Arbeit, dazu die Drucke, die bis zur Zielmenge der Artikel noch fehlen. |

### Die Spalte Verbrauch umschalten

Über der Tabelle wählst du unter **Spalte Verbrauch**, was die Spalte zeigt:

| Wahl | Bedeutung |
|---|---|
| **Ø 30 Tage** | Gramm je Tag im Schnitt der letzten 30 Tage. Die Vorgabe und die Grundlage der [Bestellvorschläge](reorder.md) und der Reichweite auf der [Übersicht](overview.md). |
| **Ø 7 Tage** | Gramm je Tag im Schnitt der letzten 7 Tage. |
| **heute**, **gestern** | Gramm, die an diesem Tag verbraucht wurden. |
| **diese Woche (KW 47)** | Gramm seit Montag dieser Kalenderwoche, heute eingeschlossen. Die Nummer ist die Kalenderwoche nach ISO; um Neujahr kann sie im alten Jahr begonnen haben (KW 53 oder KW 1). |
| **dieser Monat (November)** | Gramm seit dem Ersten dieses Monats. |
| **dieses Jahr (2026)** | Gramm seit dem 1. Januar. |
| **gesamt (seit 14.11.2026)** | Gramm seit dem ersten Tag der Aufzeichnung, der in der Wahl steht. Die Aufzeichnung wird nie gelöscht. |

Der Kopf der Spalte nennt die Wahl. Bei einem Schnitt steht dabei, über wie viele Tage gemittelt ist, solange es weniger Aufzeichnung gibt als die Tage der Wahl, zum Beispiel **Ø 30 Tage, bisher 6**. Hat eine Sorte weniger Tage als die anderen, steht ihre Zahl in ihrer Zeile (**bisher 3 Tage**). Gezählt wird überall gleich: die Abnahmen zwischen zwei Aufzeichnungen derselben Spule. Eine Abnahme gehört zum Tag der späteren Aufzeichnung; **heute** ist also die Abnahme seit der Aufzeichnung von gestern, und **diese Woche** beginnt mit der Abnahme von Sonntag auf Montag. Woche, Monat und Jahr sind Kalenderzeiträume, keine gleitenden Fenster. Eine Wahl braucht Aufzeichnungen an zwei Tagen in ihrem Zeitraum (für **heute** von gestern und heute); sonst steht „noch kein Verlauf“, auch wenn die Sorte schon länger aufgezeichnet wird, etwa nach einem Tag, an dem Bambuddy nicht erreichbar war. Alle Tage sind Tage der Aufzeichnung in der Zeitzone des Servers (siehe [Einstellungen](../operate/settings.de.md#was-nicht-auf-dieser-seite-steht)); **heute** beginnt dort um Mitternacht. Ein Browser in einer anderen Zeitzone als der Server sieht die Tage des Servers: Kurz nach Mitternacht seiner Uhr kann der Tag noch der vorige oder schon der nächste sein. Bis Version 1.2.2 aufgezeichnete Tage sind Tage in UTC und bleiben es; rund um Mitternacht können sie um einige Stunden danebenliegen.

Dein Browser merkt sich die Wahl für den nächsten Besuch, jedes Gerät seine eigene. Ohne JavaScript zeigt die Seite **Ø 30 Tage** und keine Wahl. Die Kachel **Filament** der Übersicht und die Bestellvorschläge rechnen immer mit **Ø 30 Tage**.

Die Karten oben zählen Spulensorten, Gesamtbestand, Spulen und die nachgekauften Sorten **Ohne Einstellungen**. Ein Hinweis über der Tabelle sagt, ob der Bestand **Live aus Bambuddy.** kommt oder wie alt er ist. Sind es die zuletzt aufgezeichneten Zahlen, steht der Tag außerdem unter den Karten für Bestand und Spulen und im Kopf der Spalten **Bestand** und **Spulen**, als **Stand vom …**, damit niemand sie für Live-Zahlen hält. Unter der Tabelle nennt ein Hinweis Filament-Slots, die noch keine angekreuzte Spulensorte haben; jeder Eintrag führt zur Zuordnung.

## Bestelleinstellungen einer Spulensorte

Klicke auf eine Spulensorte (oder auf das Symbol **einstellen** oder **Einstellungen ändern**), um ihre **Bestelleinstellungen** zu öffnen. Sie zählen nur für Sorten, die in irgendeinem Artikel nachgekauft werden, und sie speisen die [Bestellvorschläge](reorder.md). Sie dienen nur den Vorschlägen: Reprintbuddy bucht nie Filament, den Bestand führt Bambuddy.

![Die Bestelleinstellungen einer Spulensorte: das Sicherheitspolster und zwei Lieferanten, einer davon der Standard](../assets/images/de/filament-settings.png)

Oben stehen die Felder der Sorte selbst:

| Feld | Bedeutung | Vorgabe |
|---|---|---|
| **Sicherheitspolster** und **Einheit des Sicherheitspolsters** | Die Reserve, die nach der Lieferzeit noch übrig sein soll, 0 bis 1.000.000, in **Tagen** Verbrauch oder in **Gramm**. | 0 Tage |

Darunter, unter **Lieferanten**, ein Block je Lieferant, 1 bis 10, wie bei einem [Zukaufteil](inspection.md#lieferanten). Ein Lieferant ist **Standard**: Seine Lieferzeit entscheidet, ob ein Vorschlag erscheint; der Vorschlag nennt dann die Menge für jeden Lieferanten.

| Feld | Bedeutung | Vorgabe |
|---|---|---|
| **Name** | Freier Text, bis 200 Zeichen; darf leer bleiben, der Block heißt dann **Lieferant 1**, **Lieferant 2** usw. Beim Tippen werden Namen vorgeschlagen, die bei Spulensorten und Zukaufteilen schon vorkommen. | leer |
| **Bestell-Link** | Adresse, die mit `http://` oder `https://` beginnen muss, bis 2000 Zeichen. | leer |
| **Bestelleinheit (Gramm je Spule)** | Gramm, die eine Spule hat; in dieser Einheit verkauft dieser Lieferant die Sorte, 1 bis 100.000. Vorschläge rechnen immer in ganzen Spulen und runden auf. | 1000 |
| **Mindestbestellmenge (Spulen)** | So viele Spulen muss eine Bestellung mindestens haben, 1 bis 1000. | 1 |
| **Lieferzeit (Tage)** | Tage von der Bestellung bis zum Eintreffen, 0 bis 365. | 0 |

**Lieferant hinzufügen** fügt einen Block an, das Kreuz entfernt einen; der letzte bleibt. Entfernst du den Standard, wähle vor dem Speichern einen anderen. Gespeichert wird alles zusammen mit **Speichern**.

Ein Vorschlag erscheint, wenn der Bestand minus geplanter Bedarf minus Verbrauch in der Lieferzeit des Standard-Lieferanten unter das Sicherheitspolster fällt. Eine nachgekaufte Sorte ohne Bestelleinstellungen bekommt keinen Vorschlag; die Kennzahlen zählen sie unter **Ohne Einstellungen**.

Beim Update von einer Version vor 0.3.16 werden die bisherigen Einstellungen jeder Sorte ihr einziger Lieferant und Standard; die Vorschläge bleiben gleich. Eine Bestellung, die schon unterwegs ist, behält ihre Rollengröße.

## Wenn Bambuddy nicht erreichbar ist

Die Seiten funktionieren weiter, und es wird nichts geraten.

- Die Seite *Filament* zeigt den Bestand des zuletzt festgehaltenen Tages, mit Datum. Wurde noch nichts festgehalten, sagt sie, dass der Bestand gerade nicht bekannt ist, und zeigt Striche.
- Die Filament-Zuordnung eines Artikels zeigt ebenfalls den Bestand des zuletzt festgehaltenen Tages; im Kopf der Spalte **Bestand** steht **Stand vom …**. Ohne Aufzeichnung zeigt sie die zuletzt bekannten Spulensorten ohne Bestand.
- Die Seite *Bestellvorschläge* rechnet die Filament-Vorschläge mit dem zuletzt festgehaltenen Bestand; jede Filament-Kachel sagt an ihrem Bestand **Stand vom …**. Ohne Aufzeichnung zeigt sie keine Filament-Vorschläge, nur die für Zukaufteile.
- Hat schon die letzte Abfrage Bambuddy nicht erreicht, fragen diese Seiten nicht erneut und zeigen sofort den festgehaltenen Bestand. Live-Zahlen gibt es wieder, sobald eine Abfrage Bambuddy erreicht.

Denselben festgehaltenen Bestand zeigen die Seiten, solange ein [Update oder eine Wiederherstellung](../operate/update-backup.md) läuft. Dann ändern sie nichts: Neue Spulensorten kommen hinzu und eingetroffene Bestellungen verlassen **Unterwegs** erst beim nächsten Aufruf danach.
