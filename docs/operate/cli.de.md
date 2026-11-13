# Kommandozeile

Der Befehl `reprintbuddy` ist der Weg zurück, wenn die Weboberfläche nicht erreichbar ist: ein vergessenes Passwort, ein falscher Port, eine falsche Lauschadresse, ein Zertifikat, das nicht mehr funktioniert, oder eine Sicherung, die wiederhergestellt werden soll. Diese Seite führt die Befehle auf.

## Wie der Befehl läuft

Das Installationsskript legt den Befehl unter `/usr/local/bin/reprintbuddy` ab. Führe ihn auf dem Rechner aus, auf dem Reprintbuddy läuft, als root. Er läuft dann als Dienstbenutzer `reprintbuddy` (aus dem Ordner `/`), damit die Datenbankdateien ihren Besitzer behalten. Befehle, die nach einem Passwort fragen (`set-password`, `restore-backup`), behalten dein Terminal; die anderen laufen ohne.

Jeder Befehl nimmt `--db PFAD`, die Datenbankdatei. Vorgabe ist `/var/lib/reprintbuddy/reprintbuddy.db`, die Datenbank des installierten Dienstes; du lässt es also weg. Es ist das Einzige, was nicht in der Datenbank selbst liegt, siehe [Einstellungen](settings.md). Was ein Befehl ändert (zum Beispiel ein neues Passwort oder ein neuer Port), steht im [Protokoll](log.md) als vom **Benutzer** gemacht, mit dem Zusatz „Befehl auf dem Server“.

## Befehle für dich

| Befehl | Wirkung |
|---|---|
| `reprintbuddy --version` | Gibt die installierte Version aus, zum Beispiel `reprintbuddy 0.4.12`. |
| `reprintbuddy set-password --username <Name>` | Setzt das Passwort des Kontos. Fragt am Terminal zweimal nach dem neuen Passwort. |
| `reprintbuddy set-port <Port>` | Setzt den Port, 1 bis 65535. |
| `reprintbuddy set-host <Adresse>` | Setzt die Lauschadresse, zum Beispiel `0.0.0.0`. Wird abgelehnt, wenn dieser Rechner dort nicht lauschen kann. |
| `reprintbuddy disable-https` | Entfernt Zertifikat und Schlüssel für HTTPS: Reprintbuddy spricht wieder einfaches HTTP. |
| `reprintbuddy restore-backup <Datei>` | Stellt eine vollständige Sicherung wieder her, während der Dienst gestoppt ist. Siehe [Update und Sicherung](update-backup.md#wiederherstellen-auf-der-kommandozeile). |

`set-port`, `set-host` und `disable-https` ändern die gespeicherte Einstellung. Sie wirken nach einem Neustart:

```bash
systemctl restart reprintbuddy
```

## Vergessenes Passwort

```bash
reprintbuddy set-password --username admin
```

Der Befehl fragt nach „New password:“ und „Repeat password:“ (beim Tippen wird nichts angezeigt). Das Passwort braucht mindestens 8 Zeichen. Er endet mit „Password set for admin. All sessions have ended.“, und alle melden sich neu an. Einen Neustart brauchst du nicht.

Es gibt nur ein Konto, und `set-password` **benennt** es auch auf den angegebenen `--username` **um**. Ein vergessener Benutzername ist also kein Hindernis: Nimm den Namen, den du ab jetzt haben willst. Gibt es noch kein Konto, legt der Befehl es an.

## Falscher Port

```bash
reprintbuddy set-port 8080
systemctl restart reprintbuddy
```

Bei einem Port unter 1024 ergänzt der Befehl einen Hinweis: Der vom Installationsskript eingerichtete Dienst darf einen solchen Port belegen, ein anders gestarteter vielleicht nicht. Startet Reprintbuddy nach dem Neustart nicht, wähle einen höheren Port.

## Falsche Lauschadresse

```bash
reprintbuddy set-host 0.0.0.0
systemctl restart reprintbuddy
```

`0.0.0.0` heißt: alle Adressen des Rechners. Der Befehl lehnt eine Adresse ab, auf der dieser Rechner nicht lauschen kann („host: this machine cannot listen on that address“), und speichert nichts.

## Zertifikat funktioniert nicht mehr

```bash
reprintbuddy disable-https && systemctl restart reprintbuddy
```

Öffne Reprintbuddy danach mit `http://`. Ein neues Zertifikat kannst du unter *Einstellungen → Allgemein* hochladen, siehe [Netz und Sicherheit](network.md#https).

## Befehle des Installationsskripts und des Dienstes

Diese tippst du normalerweise nie.

| Befehl | Wofür |
|---|---|
| `reprintbuddy serve` | Führt die Weboberfläche aus. Der Dienst `reprintbuddy.service` startet ihn. |
| `reprintbuddy migrate` | Legt die Datenbank an oder bringt sie auf den neuesten Stand. |
| `reprintbuddy init --language en` (oder `de`) | Migriert und setzt die Sprache der Oberfläche. Das Installationsskript nutzt ihn. |
| `reprintbuddy update [--target <Tag>]` | Der Update-Helfer. Die Update-Unit startet ihn; um von Hand zu aktualisieren, nimm `systemctl start reprintbuddy-update.service`, siehe [Update und Sicherung](update-backup.md#update-von-hand). |
| `reprintbuddy url [--host <Host>] [--next]` | Gibt die Adresse der Weboberfläche aus; mit `--next` hängt er die Seite an, die als Nächstes zu öffnen ist. Das Installationsskript nutzt ihn. |

## Fehler

Ein falsch gestellter Befehl (ein Port, der keine Zahl ist, zwei verschiedene Passwörter, eine Adresse, die der Rechner nicht nutzen kann) gibt `reprintbuddy: …` auf stderr aus, ändert nichts und endet mit dem Exit-Code 2.

## Weiterführende Seiten

- [Netz und Sicherheit](network.md): durch HTTPS, Port oder Adresse ausgesperrt.
- [Update und Sicherung](update-backup.md): `restore-backup` im Einzelnen.
- [Einstellungen](settings.md): dieselben Einstellungen in der Oberfläche.
