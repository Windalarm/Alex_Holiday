# 🏝️ Malediven-Angebotssuche

Durchsucht einmal täglich automatisch Flüge, Hotelpreise und Pauschalangebote für einen
Malediven-Urlaub (Standard: 18.03.2027 – 18.04.2027, ab Bremen, Stuttgart, Hannover;
Referenz: Water Villa im Sun Siyam Olhuveli, South Male Atoll).

Gesucht werden nur Flüge, die **am ersten Urlaubstag früh in Malé landen** (Standard: bis 09:00 Uhr)
und bei denen du **am Abreisetag spätestens um 21:00 Uhr wieder in Deutschland** bist.

Die Ergebnisse stehen in **`REPORT.md`**. Bei guten Preisen bekommst du eine **E-Mail an die
Adressen, die du selbst festlegst**.

## Einrichtung (ca. 10 Minuten)

1. **Repository anlegen:** Auf GitHub ein neues (gern privates) Repository erstellen und
   alle Dateien hochladen – auch den Ordner `.github/workflows/`.
2. **SerpApi-Schlüssel holen:** Konto auf <https://serpapi.com> anlegen, API-Key kopieren.
   Damit werden Google Flights und Google Hotels abgefragt.
3. **Optional – Claude-Websuche:** API-Key auf <https://console.anthropic.com> erstellen
   (eigenes, nutzungsabhängig bezahltes Konto, unabhängig von einem Claude-Abo).
   Ohne Key läuft alles andere trotzdem; dann in `config.yaml` `claude_websuche.aktiv: false` setzen.
4. **Schlüssel hinterlegen:** Im Repository unter *Settings → Secrets and variables → Actions →
   New repository secret* anlegen:
   - `SERPAPI_KEY`
   - `ANTHROPIC_API_KEY` (optional)
   - E-Mail-Versand (siehe nächster Abschnitt): `EMAIL_EMPFAENGER`, `SMTP_SERVER`, `SMTP_PORT`,
     `SMTP_BENUTZER`, `SMTP_PASSWORT`, optional `EMAIL_ABSENDER`
5. **Rechte prüfen:** *Settings → Actions → General → Workflow permissions* →
   „Read and write permissions“ auswählen.
6. **Erster Test:** Reiter *Actions* → „Malediven-Angebotssuche“ → *Run workflow* →
   Häkchen bei „Testmail mit aktuellem Bericht senden“ setzen → Start. Kommt die Mail an,
   ist alles richtig eingerichtet.

## E-Mail-Adressen festlegen

**Empfänger:** Secret `EMAIL_EMPFAENGER` anlegen und die Adressen durch Komma getrennt eintragen, z. B.

```
ich@example.de, partner@example.de
```

Ändern kannst du das jederzeit, indem du das Secret mit neuem Inhalt überschreibst. Secrets sind
nicht öffentlich sichtbar, deshalb ist das sicherer als ein Eintrag in `config.yaml`.

**Absender:** Die Mails werden über ein normales E-Mail-Postfach verschickt, z. B. dein eigenes.
Dafür brauchst du dessen SMTP-Zugangsdaten:

| Anbieter | `SMTP_SERVER` | `SMTP_PORT` | Hinweis |
|---|---|---|---|
| Gmail | `smtp.gmail.com` | `587` | Bestätigung in zwei Schritten aktivieren und ein **App-Passwort** erstellen, dieses als `SMTP_PASSWORT` verwenden |
| GMX | `mail.gmx.net` | `587` | In den GMX-Einstellungen „POP3/IMAP Abruf“ bzw. externen Zugriff erlauben |
| WEB.DE | `smtp.web.de` | `587` | In den Einstellungen „POP3/IMAP Abruf“ erlauben |
| Outlook/Hotmail | `smtp-mail.outlook.com` | `587` | Je nach Konto ist SMTP-Anmeldung nur mit App-Passwort möglich |

`SMTP_BENUTZER` ist meist die vollständige E-Mail-Adresse des Absenderpostfachs. Die Angaben
können sich bei den Anbietern ändern, im Zweifel in deren Hilfe nach „SMTP“ suchen.

**Wann kommt eine Mail?** In `config.yaml` unter `benachrichtigung`:
`bei_alarm: true` (nur bei guten Preisen) und/oder `taeglicher_bericht: true` (jeden Tag).

## Flugzeiten

In `config.yaml` unter `flugzeiten`:

- `hinflug_ankunft_male_bis` – späteste Landung in Malé am ersten Urlaubstag (Ortszeit Malé,
  4 h vor deutscher Winterzeit bzw. 3 h vor Sommerzeit)
- `rueckflug_ankunft_deutschland_bis` – späteste Landung am Heimatflughafen am Abreisetag
- `rueckflug_details_laden` – prüft den Rückflug ganz genau (kostet eine Suche mehr pro Flughafen)

Im Bericht stehen für jeden Flug Abflug- und Ankunftszeiten für Hin- und Rückflug.

## Reisezeit und Abflughäfen variieren

**Dauerhaft:** `config.yaml` bearbeiten (direkt im Browser auf GitHub möglich):

- `reise.fruehester_abflug` / `reise.spaetester_rueckflug` – Suchfenster
- `reise.naechte` – z. B. `[7, 10, 12, 14]`
- `reise.schrittweite_tage` – wie dicht die Abflugtage geprüft werden
- `abflughaefen` – IATA-Codes, z. B. `[BRE, STR, HAJ, FRA, HAM]`
- `hotels` – weitere Resorts mit `aktiv: true` einschalten
- `alarm` – deine Preisgrenzen

**Einmalig:** Beim manuellen Start über *Run workflow* kannst du Abflughäfen, Nächte und
Zeitraum für diesen einen Lauf überschreiben, ohne die Konfiguration zu ändern.

## Suchkontingent

Jede Abfrage bei SerpApi zählt als eine Suche. Pro Tag:

`termine_pro_lauf × (Abflughäfen × 2 + aktive Hotels)` (× 1 statt × 2, wenn `rueckflug_details_laden: false`)

Mit den Standardwerten sind das 1 × (3 × 2 + 1) = 7 Suchen/Tag, also rund 210 im Monat. Das Skript
rotiert dabei durch alle Termin-Kombinationen, so dass nach einigen Tagen jeder Termin geprüft
ist und danach wieder aktualisiert wird. Prüfe auf serpapi.com, wie groß dein Kontingent ist,
und passe `termine_pro_lauf` oder `schrittweite_tage` entsprechend an.

## Gut zu wissen

- Von Bremen, Stuttgart und Hannover gibt es keine Direktflüge nach Malé – gefunden werden
  Umsteigeverbindungen (z. B. über Frankfurt, Istanbul, Dubai, Doha).
- Flugpreise gelten für alle Reisenden zusammen, Hotelpreise für den gesamten Aufenthalt.
- Ob die Water Villa einzeln ausgewiesen wird, hängt davon ab, was Google Hotels für das Datum
  anzeigt. Fehlt sie, steht im Bericht der günstigste Zimmerpreis des Resorts.
- Pauschalangebote aus der Claude-Websuche sind Fundstellen aus dem Netz – Preis und
  Verfügbarkeit immer auf der verlinkten Seite prüfen.
- GitHub deaktiviert geplante Workflows in Repositories, die 60 Tage keine Aktivität hatten.
  Die täglichen Ergebnis-Commits halten das Repository normalerweise aktiv; falls nicht, im
  Reiter *Actions* wieder einschalten.
- Nach dem letzten möglichen Abflugtermin sucht das Skript automatisch nichts mehr.
