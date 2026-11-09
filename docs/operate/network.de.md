# Netz und Sicherheit

Reprintbuddy ist für dein Heimnetz gedacht. Diese Seite sagt, wie es erreicht werden soll, wie du Adresse, Port und HTTPS einstellst, wie du es hinter einem Reverse-Proxy betreibst, was du tust, wenn du dich aussperrst, und wie deine Schlüssel und Daten geschützt sind.

## Heimnetz und VPN

Einfaches HTTP genügt im Heimnetz und über ein VPN. Mehr braucht Reprintbuddy nicht.

!!! warning "Reprintbuddy nie direkt ins Internet stellen"
    Keine Portfreigabe, kein öffentlicher Reverse-Proxy. Für den Zugriff von außen nimm ein VPN in dein Netz, zum Beispiel das VPN deines Routers, WireGuard oder Tailscale. Über das VPN verhält es sich wie in deinem eigenen Netz.

## Adresse und Port

Unter *Einstellungen → Allgemein* hat die Karte **Server** zwei Felder:

- **Lauschadresse**: die Adresse, auf der Reprintbuddy lauscht. Die Vorgabe `0.0.0.0` heißt: alle Adressen des Rechners. Reprintbuddy speichert nur eine Adresse, auf der dieser Rechner wirklich lauschen kann, denn eine falsche würde dich nach dem Neustart aussperren.
- **Port**: der Port der Weboberfläche, 1 bis 65535, Vorgabe `8080`. Ports unter 1024 funktionieren mit dem vom Installationsskript eingerichteten Dienst; ein anders gestarteter Dienst darf sie vielleicht nicht belegen.

Änderungen an Adresse, Port und HTTPS wirken **erst nach einem Neustart**, siehe [Neustart](update-backup.md#neustart). Bis dahin sagt die Seite das weiter. Nach dem Neustart nennt die Neustart-Seite die neue Adresse; öffne Reprintbuddy dort und ändere deine Lesezeichen.

Ein gedruckter QR-Code behält die Adresse, die beim Druck galt. Ändern sich Adresse oder Port, drucke die Etiketten neu. Reprintbuddy findet seine eigene Netzwerkadresse für die Codes, ohne etwas zu senden; wird es über einen Namen oder einen Proxy erreicht, trage diese Adresse unter *Einstellungen → Allgemein → QR-Codes* ein, siehe [Etiketten und QR-Codes](../use/labels.md).

## HTTPS

HTTPS ist optional. Du kannst Reprintbuddy selbst ein Zertifikat geben oder es hinter einem Reverse-Proxy betreiben, der TLS übernimmt.

So nutzt du ein Zertifikat:

1. Öffne *Einstellungen → Allgemein*, Karte **Server**, Abschnitt **HTTPS (optional)**.
2. Füge das Zertifikat in **Zertifikat (PEM)** und den Schlüssel in **Privater Schlüssel (PEM)** ein oder wähle die Dateien. Jedes ist höchstens 64 KB groß.
3. Drücke **Speichern**. Reprintbuddy prüft, ob Zertifikat und Schlüssel zusammenpassen und sich laden lassen. Wenn nicht, wird nichts gespeichert, und die Seite sagt, warum. Das Ablaufdatum und den Namen im Zertifikat prüft es nicht.
4. Starte Reprintbuddy neu. Es antwortet dann mit HTTPS.

Beides ist nötig, und der private Schlüssel darf kein Passwort haben („Passwortgeschützte private Schlüssel werden nicht unterstützt.“). Zertifikat und Schlüssel liegen in der Datenbank. Der Schlüssel wird nie wieder angezeigt: Das Feld zeigt `********`, und ein leeres Feld lässt den gespeicherten Schlüssel.

Um zu HTTP zurückzukehren, hake **Zertifikat und privaten Schlüssel entfernen (einfaches HTTP)** an, drücke **Speichern** und starte neu. Das Kästchen erscheint nur, solange ein Zertifikat gespeichert ist.

## Reverse-Proxy

Du kannst Reprintbuddy auch hinter einem Reverse-Proxy betreiben, den du schon hast. Reprintbuddy bleibt bei HTTP, und der Proxy übernimmt TLS.

- Läuft der Proxy auf **demselben System**, setze die Lauschadresse auf `127.0.0.1`, damit Reprintbuddy nicht am Proxy vorbei erreichbar ist.
- Gib Reprintbuddy auf dem Proxy **eine eigene Adresse oder einen eigenen Port**, zum Beispiel `https://reprintbuddy.example.net/` oder `https://example.net:8443/`.
- Ein **Unterpfad** wie `https://example.net/reprintbuddy/` funktioniert **nicht**: Links und Weiterleitungen würden am Proxy vorbeizeigen. Reprintbuddy schreibt eine Warnung ins Systemprotokoll, einmal, wenn es so erreicht wird (ein Root-Pfad oder ein Header `X-Forwarded-Prefix`). Das Systemprotokoll liest du mit `journalctl -u reprintbuddy`.
- Die Seiten aktualisieren sich über einen Datenstrom unter `/live` (Server-Sent Events). Der Proxy muss ihn ohne Pufferung weiterreichen; Reprintbuddy sendet den Header `X-Accel-Buffering: no`, den nginx beachtet.
- Trage die Adresse des Proxys unter *Einstellungen → Allgemein → QR-Codes* ein, damit die QR-Codes sie tragen.

## Ersteinrichtung und Namen aus öffentlichen Domains

Solange es kein Admin-Konto gibt, könnte jeder, der die Seite erreicht, es anlegen. Eine Webseite im Internet könnte den Browser eines Besuchers dazu bringen, dein Netz anzusprechen, indem sie ihren eigenen Namen auf eine private Adresse zeigen lässt (DNS-Rebinding). Deshalb nimmt Reprintbuddy bei der Ersteinrichtung nur diese Adressen in der Adresszeile des Browsers an:

- eine IP-Adresse (mit oder ohne Port),
- `localhost`,
- einen Namen ohne Punkt,
- einen Namen, der auf `.local`, `.lan`, `.home`, `.internal`, `.localdomain` oder `.home.arpa` endet.

Jeder andere Name, zum Beispiel ein Name aus einer öffentlichen Domain wie ein VPN-DNS-Name, bekommt die Seite „Adresse nicht erlaubt“: „Solange Reprintbuddy eingerichtet wird, öffne es über seine IP-Adresse …“. Öffne Reprintbuddy über seine IP-Adresse, schließe die Einrichtung ab und benutze den Namen danach. Das gilt auch für einen Namen auf einem Reverse-Proxy: Richte zuerst über die IP-Adresse ein.

## Wenn du ausgesperrt bist

Ein Zertifikat, das nicht mehr funktioniert (abgelaufen, falscher Name), ein falscher Port oder eine falsche Lauschadresse können dich aussperren. Auf dem System, als root:

```bash
reprintbuddy disable-https && systemctl restart reprintbuddy
reprintbuddy set-port 8080 && systemctl restart reprintbuddy
reprintbuddy set-host 0.0.0.0 && systemctl restart reprintbuddy
```

Der erste schaltet HTTPS aus, der zweite setzt den Port, der dritte die Lauschadresse. Nimm den, den du brauchst. Alle Befehle stehen unter [Kommandozeile](cli.md). Auch ein vergessenes Passwort lässt sich dort beheben.

## Sicherheit

- **Nicht im Internet.** Siehe oben: keine Portfreigabe, kein öffentlicher Reverse-Proxy; nimm ein VPN.
- **Schlüssel, Passwörter und Token.** API-Schlüssel, der private Schlüssel eines HTTPS-Zertifikats, das Token von Home Assistant und Passwörter sind nur bei der Eingabe sichtbar. Gespeichert zeigt die Oberfläche `********`. Ein leeres Feld lässt den gespeicherten Wert, eine neue Eingabe ersetzt ihn. Passwörter werden nur als Hash gespeichert.
- **Token von Home Assistant.** Es handelt mit allen Rechten seines Home-Assistant-Benutzers, lege es also für einen Benutzer ohne Admin-Recht an. Es geht nur an die Adresse, mit der es eingegeben wurde, samt Pfad; für eine neue Adresse gib es neu ein. Push-Nachrichten aufs Handy laufen über den Push-Dienst von Google bzw. Apple und nennen Artikel, Spulensorten und Zukaufteile beim Namen, solange **Namen in Nachrichten nennen** an ist.
- **Jede Änderung braucht eine Sitzung und ein Token.** Eine Änderung wird nur aus einer angemeldeten Sitzung und mit einem CSRF-Token angenommen.
- **Die Datenbankdatei** ist nur für den Dienstbenutzer lesbar. Wer auf dem Rechner root ist, kommt an sie heran. Dasselbe gilt für die Kopien in `backups/` und für eine vollständige Sicherung ohne Passwort, die die Schlüssel enthalten.

Melde Sicherheitslücken, wie in [SECURITY.md](https://github.com/Eifel-Joe/reprintbuddy/blob/main/SECURITY.md) beschrieben: privat über GitHub, nie als öffentliches Issue. Nur die neueste Version erhält Sicherheitskorrekturen.

## Weiterführende Seiten

- [Einstellungen](settings.md): alle Einstellungen.
- [Update und Sicherung](update-backup.md): Neustart, Sicherungen.
- [Kommandozeile](cli.md): Befehle für den Notfall.
