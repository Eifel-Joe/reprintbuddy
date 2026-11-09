# Fehlersuche

Ist etwas nicht in Ordnung, sagt Reprintbuddy es im Klartext. Diese Seite listet die Warnungen, was sie bedeuten und was zu tun ist, und was du tust, wenn ein Update scheitert oder du ausgesperrt bist.

## Meldungen oben auf der Seite

Nach den meisten Aktionen zeigt die Seite oben eine Meldung, zum Beispiel dass etwas gespeichert oder gebucht wurde. Gute Nachrichten verschwinden nach etwa 8 Sekunden von selbst; sie bleiben stehen, solange der Mauszeiger oder der Tastaturfokus auf ihnen ist. Ärger (eine Warnung oder ein Fehler) bleibt, bis du ihn mit seinem Knopf **×** wegmachst (für Screenreader **Meldung schließen** genannt); der Fokus geht dann zum Knopf der nächsten Meldung oder zum Hauptteil der Seite, statt nach oben zu springen. Verlangt dein System weniger Bewegung, verschwindet eine Meldung ohne Ausblenden.

## Wo Warnungen erscheinen

Die Warnungen stehen auf der *Übersicht* und auf der Seite *Warteschlange*, jeweils als Karte. Auf der Warteschlange steht darunter **Letzte erfolgreiche Abfrage bei Bambuddy: …** (oder „Bambuddy wurde noch nicht erfolgreich abgefragt.“); die Übersicht zeigt das in ihrer Kopfzeile als **Letzte Abfrage**, neben dem Zustand der Verbindung (siehe [Übersicht](../use/overview.md)). Der Knopf **Jetzt bei Bambuddy abfragen** fragt Bambuddy sofort, statt auf die nächste reguläre Abfrage zu warten; die meisten Warnungen verschwinden von selbst bei der nächsten Abfrage, die gelingt.

Eine **rote** Karte heißt: Bambuddy antwortet nicht oder lehnt ab, eine Übergabe ist unklar, oder eine Push-Nachricht ging nicht durch. Eine **gelbe** Karte ist ein Zustand, den du gewählt hast oder der auf dich wartet.

## Rote Warnungen

| Warnung | Was sie bedeutet | Was zu tun ist |
|---|---|---|
| Bambuddy ist nicht erreichbar. Es wird nichts übergeben und kein Bestand geändert. | Die letzte Abfrage bekam keine Antwort: Bambuddy ist aus, seine Adresse hat sich geändert, oder das Netz ist unterbrochen. | Prüfe, ob Bambuddy läuft und das Reprintbuddy-System es erreicht. Prüfe die Adresse unter *Einstellungen → Bambuddy*. Gelingt die nächste Abfrage, verschwindet die Warnung. |
| Bambuddy lehnt die Abfrage ab (`…`). Fertige Drucke werden erst danach gebucht. | Bambuddy hat geantwortet, aber die reguläre Abfrage abgelehnt. Der Text in Klammern ist Bambuddys Grund, zum Beispiel ein fehlendes Recht oder ein gelöschter Schlüssel. | Drücke unter *Einstellungen → Bambuddy* **Prüfen und speichern** (ein leeres Schlüsselfeld behält den gespeicherten Schlüssel). Die Seite nennt den Besitzer des Schlüssels und jedes fehlende Recht. Behebe es wie unter [Ersteinrichtung](../start/setup.md#fehlende-rechte-spater) beschrieben. |
| Bambuddy hat einen Druck von `…` abgelehnt (`…`). Er liegt wieder in der Warteschlange; die nächste reguläre Abfrage versucht es erneut. | Bambuddy hat einen Auftrag, den Reprintbuddy übergeben hat, nicht genommen. Der Text in Klammern ist Bambuddys Grund. | Lies den Grund und behebe, was er nennt, zum Beispiel das Druckziel oder die Druckdatei dieses Artikels in Bambuddy. Die nächste reguläre Abfrage versucht es erneut. |
| `n` Übergabe(n) unklar – bis sie geklärt sind, wird nichts Neues übergeben. | Reprintbuddy hat einen Auftrag gesendet, aber keine klare Antwort bekommen (Zeitüberschreitung, Verbindung weg). Einen solchen Auftrag sendet es nie blind noch einmal. | Warte auf die nächste reguläre Abfrage: Reprintbuddy sucht seinen Eintrag in Bambuddys Warteschlange. Findet es genau einen, macht es von selbst weiter. Findet es keinen, wird der Artikel pausiert („eine Übergabe an Bambuddy ist unklar“, siehe unten); findet es mehrere, kommt die Warnung zu mehreren Einträgen. Scheitert die Suche selbst, kommt eine der beiden Warnungen nach dieser. |
| Die Suche nach einer unklaren Übergabe ist gescheitert (`…`). Bis sie geklärt ist, wird nichts Neues übergeben. | Die Suche in Bambuddys Warteschlange wurde abgelehnt. | Bring die Verbindung zu Bambuddy in Ordnung wie bei „Bambuddy lehnt die Abfrage ab“. |
| Reprintbuddy kennt seinen Bambuddy-Benutzer nicht und kann eine unklare Übergabe deshalb nicht suchen. Bis sie geklärt ist, wird nichts Neues übergeben. Unter Einstellungen die Verbindung zu Bambuddy prüfen. | Der Name des Schlüsselbesitzers ist nicht gespeichert. | Drücke unter *Einstellungen → Bambuddy* **Prüfen und speichern**. |
| Eine unklare Übergabe ist nicht eindeutig zuzuordnen: in Bambuddy kommen mehrere Einträge in Frage (Einträge `…`). Keiner wurde übernommen, der Artikel ist pausiert. In Bambuddy nachsehen und überzählige Einträge löschen; lief der Druck, über „Einbuchen ohne Druck“ buchen. | Mehrere Einträge in Bambuddys Warteschlange sehen aus wie der verlorene (gleicher Benutzer, gleiche Datei und Platte, nach dem Senden angelegt). Reprintbuddy rät nicht. | Lösche in Bambuddy die überzähligen Einträge (Reprintbuddy ändert nie Einträge in Bambuddy). Lief der Druck, buche die Stücke auf der Seite *Buchen* mit **Einbuchen ohne Druck**. Hebe dann die Pause des Artikels auf. |
| Home Assistant nicht erreichbar – die letzte Push-Nachricht ging nicht durch: `Grund` | Die letzte Push-Nachricht wurde nicht angenommen. Der Grund sagt warum: Token abgelehnt (401), Dienst unbekannt oder Daten falsch (400), Absender gesperrt (403), Fehler in Home Assistant (5xx), auf eine andere Adresse umgeleitet, nicht erreichbar oder unklar (keine Antwort in der Zeit). | Prüfe Adresse, Token und Dienst unter *Einstellungen → Benachrichtigungen* und sende eine Testnachricht. Die Warnung verschwindet, wenn eine Nachricht angenommen wird, wenn Home Assistant bei einer regulären Abfrage wieder antwortet oder wenn die Nachrichten abgeschaltet sind. Bestand und Drucken laufen unverändert weiter. Siehe [Push-Nachrichten](../use/notifications.md). |
| Home Assistant lehnt das Token ab – Reprintbuddy sendet nichts mehr, bis das Token unter Einstellungen → Benachrichtigungen neu eingetragen und gespeichert ist. | Eine reguläre Push-Nachricht oder die Prüfung, ob Home Assistant antwortet, bekam **Token abgelehnt (401)**. Jeder weitere Versuch würde in Home Assistant als Fehlanmeldung zählen, darum hat Reprintbuddy angehalten. | Trage unter *Einstellungen → Benachrichtigungen* ein richtiges Token ein und speichere. Der Halt gilt, bis die Karte gespeichert wird, auch über einen Neustart. Siehe [Push-Nachrichten](../use/notifications.md#sicherheit). |

## Gelbe Warnungen

| Warnung | Was sie bedeutet | Was zu tun ist |
|---|---|---|
| Bambuddy ist noch nicht eingerichtet. Reprintbuddy plant Nachdrucke, übergibt aber nichts. | Es ist keine Verbindung zu Bambuddy gespeichert. | Binde Bambuddy an, siehe [Ersteinrichtung](../start/setup.md). |
| Bambuddy meldet einen Zustand, den Reprintbuddy nicht kennt (`…`). Diese Drucke bleiben, wie sie sind. | Ein Druck hat einen Zustand, den diese Version von Reprintbuddy nicht kennt, zum Beispiel weil Bambuddy neuer ist. | Sieh dir diese Drucke in Bambuddy an. Reprintbuddy lässt sie, wie sie sind. Ist Bambuddy neuer als dein Reprintbuddy, aktualisiere Reprintbuddy (siehe [Update und Sicherung](../operate/update-backup.md)). |
| `n` Druck(e) warten in Bambuddy auf Freigabe. Diese in Bambuddy starten. Damit spätere Drucke nicht warten, braucht die Gruppe des Benutzers, dem der API-Schlüssel von Reprintbuddy gehört, dort das Recht „Print Without Review“ (queue:start_unreviewed) oder „Update All Queue“ (queue:update_all). | Drucke, die Reprintbuddy übergeben hat, hält Bambuddy zurück, bis jemand sie startet. | Starte sie in Bambuddy. Gib der Gruppe `Reprintbuddy` das Recht **Print Without Review**, siehe [Ersteinrichtung](../start/setup.md). |
| Die Automatik ist mit einem Fehler stehen geblieben (`…`). Sie versucht es beim nächsten Durchlauf erneut. | Ein unerwarteter Fehler im Durchlauf der Automatik. | Warte den nächsten Durchlauf ab. Bleibt die Warnung, sieh ins Systemprotokoll (`journalctl -u reprintbuddy`) und melde es als Issue auf [GitHub](https://github.com/Eifel-Joe/reprintbuddy/issues), ohne Schlüssel und Passwörter. |
| Eine Erweiterung ist bei der letzten Abfrage fehlgeschlagen: `filament`. | Der Teil, der die tägliche Aufzeichnung des Filamentbestands führt und gelieferte Spulen erkennt, ist bei der letzten regulären Abfrage fehlgeschlagen. | Prüfe, ob Bambuddy (und Spoolman, falls du es nutzt) erreichbar ist und deine Spulen zeigt. Die nächste Abfrage versucht es erneut. Siehe [Filament](../use/filament.md). |
| Ein Update läuft. Die Automatik wartet, bis es fertig ist. | Ein Update ist im Gang; bis es fertig ist, ist jede Änderung gesperrt. | Warte. Siehe [Update und Sicherung](../operate/update-backup.md). |
| Die Warteschlange ist angehalten. Nachdrucke werden geplant, aber nicht übergeben. | Der Schalter **Warteschlange anhalten (nichts wird an Bambuddy übergeben)** ist an. | Schalte ihn unter *Einstellungen → Automatik* aus, wenn Reprintbuddy wieder übergeben soll. Siehe [Automatik](../use/automation.md). |
| Obergrenze von `n` offenen Drucken in Bambuddy erreicht; der Rest wartet in Reprintbuddy. | Reprintbuddy hat so viele Drucke in Bambuddy offen (eingereiht oder im Druck), wie die Obergrenze erlaubt. Das ist normal. | Nichts; der nächste Eintrag wird übergeben, sobald ein Druck fertig ist. Willst du mehr erlauben, erhöhe **Obergrenze offener Drucke in Bambuddy** unter *Einstellungen → Automatik*. |
| Wartet auf einen freien Drucker (Wartung in Bambuddy): `Artikel`. | Der Drucker des Druckziels dieser Artikel ist in Bambuddy im Wartungsmodus, oder es sind alle Drucker seines Modells. | Beende den Wartungsmodus in Bambuddy oder wähle ein anderes Druckziel für den Artikel. Der Artikel wird nicht pausiert. |

## Pausierte Artikel

Ein pausierter Artikel hat eine eigene Karte: der Name des Artikels, dann der Grund. Solange ein Artikel pausiert ist, legt die Automatik für ihn nichts an und übergibt nichts. Jede Karte bietet den Weg weiter an:

| Angezeigter Grund | Was passiert ist | Was zu tun ist |
|---|---|---|
| Pausiert: ein Nachdruck-Eintrag wurde verringert oder gelöscht. Wer dauerhaft weniger will, senkt Mindestbestand oder Puffer. | Du hast einen Eintrag, den die Automatik in Reprintbuddys Warteschlange angelegt hat, verringert oder gelöscht. Sonst legte die Regel ihn sofort wieder an. | Willst du auf Dauer weniger, senke Mindestbestand oder Puffer (**Einstellungen bearbeiten**). Drücke dann **Pause aufheben**. |
| Pausiert: ein Reprintbuddy-Druck wurde in Bambuddy abgebrochen. | Du hast einen Druck dieses Artikels in Bambuddy abgebrochen. Die Pause verhindert, dass Reprintbuddy gegen dich arbeitet. | Drücke **Pause aufheben**, wenn die Automatik weitermachen soll. |
| Pausiert: ein Reprintbuddy-Druck ist in Bambuddy verschwunden (gelöscht oder Verlauf geleert). War er fertig, über „Einbuchen ohne Druck“ buchen. | Bambuddy kennt einen Druck, den Reprintbuddy übergeben hat, nicht mehr. | War er fertig, buche die Stücke auf der Seite *Buchen* mit **Einbuchen ohne Druck**. Drücke dann **Pause aufheben**. |
| Pausiert: zwei Drucke hintereinander sind fehlgeschlagen. | Zwei Drucke dieses Artikels sind nacheinander fehlgeschlagen. | Suche die Ursache in Bambuddy (Drucker, Datei, Filament). Drücke dann **Pause aufheben**; das setzt auch den Zähler für Fehlschläge zurück. |
| Pausiert: die Druckdatei wurde in Bambuddy geändert. Bestätigen oder neu wählen. | Prüfsumme oder Name der Druckdatei weichen von dem ab, was Reprintbuddy gespeichert hat. | **Datei bestätigen** (auf der Artikelseite **Druckdatei bestätigen**) übernimmt die neue Fassung, wie sie jetzt ist, samt Platten und Filament. **Andere Datei wählen** (**Druckdatei neu wählen**) wählt eine Datei neu. Haben sich Material oder Farbe geändert, kreuze die Spulensorten der genannten Slots neu an. Beides hebt die Pause auf. Dateien in externen Ordnern werden nicht geprüft, siehe [Grenzen](limits.md). |
| Pausiert: die Druckdatei ist in Bambuddy nicht mehr da. Datei neu wählen. | Die Datei wurde in Bambuddy gelöscht. | **Andere Datei wählen** und eine neue Datei samt Platte aussuchen. Das hebt auch die Pause auf. |
| Pausiert: der Artikel hat kein Druckziel. Eines setzen. | Der Artikel hat kein Druckermodell und keinen Drucker, oder der Drucker wurde aus Bambuddy entfernt. | **Einstellungen bearbeiten** und ein Druckziel setzen. Vorher geht **Pause aufheben** nicht. |
| Pausiert: eine Übergabe an Bambuddy ist unklar. In Bambuddys Warteschlange und Verlauf nachsehen; lief der Druck, über „Einbuchen ohne Druck“ buchen. | Reprintbuddy konnte nicht klären, ob Bambuddy einen Auftrag genommen hat. | Sieh in Bambuddys Warteschlange und Verlauf nach. Lief der Druck, buche die Stücke mit **Einbuchen ohne Druck**. Drücke dann **Pause aufheben**. |

**Zum Artikel** führt zum Artikel, wo dieselben Knöpfe im Abschnitt **Automatik pausiert** stehen.

## Weitere Hinweise

- **Bambuddy-Recht fehlt** (ein Hinweisfenster, oder nach **Später** eine Warnzeile oben): Dem gespeicherten Schlüssel fehlt ein Recht, das diese Version braucht. Siehe [Ersteinrichtung](../start/setup.md#fehlende-rechte-spater).
- **Filament nicht zugeordnet** (am Artikel) oder „Diese Filament-Slots haben noch keine angekreuzte Spulensorte, ihr Bedarf wird nicht gezählt“ (auf der Filamentseite): Bei einem Filament-Slot des Artikels ist keine Spulensorte angekreuzt. Ordne sie am Artikel zu, siehe [Filament](../use/filament.md). Ein solcher Slot zählt nicht für Bestand und Bedarf.
- **Bestand vom … (Bambuddy ist nicht erreichbar)**: Die Filamentzahlen sind die zuletzt bekannten, weil Bambuddy gerade nicht erreichbar ist. Der Tag steht noch einmal unter den Karten und im Kopf der Spalten, als **Stand vom …**.

## Ein Update ist gescheitert oder wurde unterbrochen

Unter *Einstellungen → Update und Sicherung* nennt die Karte Update den Zustand und den Schritt, der gescheitert ist. Das [Protokoll](../operate/log.md) (*Einstellungen → Protokoll*) zeigt das Ergebnis jedes Updates. Einzelheiten stehen im Systemprotokoll des Updates: `journalctl -u reprintbuddy-update`.

- **Update fehlgeschlagen, nichts wurde geändert** und **Update fehlgeschlagen, vorige Version wiederhergestellt:** Die laufende Version ist wie vorher, und die Datenbank wurde bei Bedarf wiederhergestellt. Behebe die Ursache, die die Seite nennt (oft das Netz: GitHub, PyPI), und starte das Update erneut.
- **Update wurde unterbrochen** oder **Update fehlgeschlagen, vorige Version ließ sich nicht wiederherstellen:** Führe das Installationsskript noch einmal als root aus (der Befehl von [Installation](../start/install.md); bricht es ab, weil es nicht zur installierten Version passt, nimm den Tag, den es nennt). Es repariert Pakete, Units und den Dienst und behält die Datenbank. Startet Reprintbuddy danach immer noch nicht, stelle die Sicherung wieder her, die die Seite nennt, und führe das Installationsskript noch einmal aus. Das Wiederherstellen steht unter [Update und Sicherung](../operate/update-backup.md); ohne laufende Oberfläche nimm den Befehl `reprintbuddy restore-backup` ([Kommandozeile](../operate/cli.md)).

## Ausgesperrt

Kommst du nicht hinein oder erreichst die Seite nicht, nimm die Kommandozeile auf dem Reprintbuddy-System. Alle Befehle stehen unter [Kommandozeile](../operate/cli.md).

- **Passwort oder Benutzername vergessen:** `reprintbuddy set-password --username <name>` setzt ein neues Passwort und benennt das Konto in den angegebenen Namen um.
- **Falscher Port oder falsche Lauschadresse:** `reprintbuddy set-port 8080` oder `reprintbuddy set-host 0.0.0.0`, danach `systemctl restart reprintbuddy`.
- **Ein Zertifikat, das nicht mehr funktioniert** (abgelaufen, falscher Name): `reprintbuddy disable-https && systemctl restart reprintbuddy`.

Siehe auch [Netz und Sicherheit](../operate/network.md).
