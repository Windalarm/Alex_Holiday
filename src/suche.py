#!/usr/bin/env python3
"""
Tägliche Malediven-Angebotssuche.

- Flüge (Google Flights) und Hotelpreise (Google Hotels) über SerpApi
- optional: Websuche nach Pauschalangeboten über die Claude API
- schreibt REPORT.md, speichert den Preisverlauf in data/ und erzeugt
  bei Treffern data/alarm.md (daraus macht der Workflow ein GitHub-Issue)
"""
import hashlib
import json
import os
import smtplib
import sys
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import markdown
import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config.yaml"
DATA = ROOT / "data"
VERLAUF = DATA / "verlauf.json"
STATUS = DATA / "status.json"
ALARM = DATA / "alarm.md"
REPORT = ROOT / "REPORT.md"

SERPAPI_URL = "https://serpapi.com/search.json"
KLASSEN = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
FLUGHAFEN_NAMEN = {
    "BRE": "Bremen", "STR": "Stuttgart", "HAJ": "Hannover", "FRA": "Frankfurt",
    "MUC": "München", "HAM": "Hamburg", "DUS": "Düsseldorf", "BER": "Berlin",
    "CGN": "Köln/Bonn", "NUE": "Nürnberg", "MLE": "Malé",
}


# ---------------------------------------------------------------- Hilfen
def lade_json(pfad, standard):
    try:
        return json.loads(pfad.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return standard


def speichere_json(pfad, daten):
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps(daten, ensure_ascii=False, indent=2), encoding="utf-8")


def euro(x):
    return "–" if x is None else f"{x:,.0f} €".replace(",", ".")


def de(iso):
    return date.fromisoformat(iso).strftime("%d.%m.%Y")


def fh(code):
    return f"{FLUGHAFEN_NAMEN.get(code, code)} ({code})"


def jetzt():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def uhrzeit(text):
    """'09:30' → (9, 30)"""
    h, m = text.strip().split(":")
    return int(h), int(m)


def zeitpunkt(text):
    """SerpApi-Zeit '2027-03-19 7:35' → ('2027-03-19', (7, 35)), oder None"""
    try:
        tag, zeit = text.strip().split(" ")
        return tag, uhrzeit(zeit)
    except (AttributeError, ValueError):
        return None


def ankunft_ok(segmente, tag, bis):
    """Letztes Segment landet am Tag `tag` spätestens um `bis` (h, m)."""
    if not segmente:
        return False
    zp = zeitpunkt(segmente[-1].get("arrival_airport", {}).get("time", ""))
    return bool(zp) and zp[0] == tag and zp[1] <= bis


def hhmm(segment, feld):
    zp = zeitpunkt(segment.get(feld, {}).get("time", "")) if segment else None
    return f"{zp[1][0]:02d}:{zp[1][1]:02d}" if zp else "?"


def route_von(segmente):
    if not segmente:
        return ""
    return " → ".join([segmente[0]["departure_airport"]["id"]]
                      + [s["arrival_airport"]["id"] for s in segmente])


def betrag(feld):
    """Liest einen Zahlenwert aus den verschiedenen SerpApi-Preisfeldern."""
    if isinstance(feld, (int, float)):
        return float(feld)
    if isinstance(feld, dict):
        for k in ("extracted_lowest", "extracted", "extracted_before_taxes_fees"):
            if isinstance(feld.get(k), (int, float)):
                return float(feld[k])
    return None


# ---------------------------------------------------------- Konfiguration
def lade_config():
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    env = os.environ
    # Überschreibungen beim manuellen Start ("Run workflow")
    if env.get("ABFLUGHAEFEN", "").strip():
        cfg["abflughaefen"] = [a.strip().upper() for a in env["ABFLUGHAEFEN"].split(",") if a.strip()]
    if env.get("NAECHTE", "").strip():
        cfg["reise"]["naechte"] = [int(n) for n in env["NAECHTE"].split(",") if n.strip()]
    if env.get("FRUEHESTER_ABFLUG", "").strip():
        cfg["reise"]["fruehester_abflug"] = env["FRUEHESTER_ABFLUG"].strip()
    if env.get("SPAETESTER_RUECKFLUG", "").strip():
        cfg["reise"]["spaetester_rueckflug"] = env["SPAETESTER_RUECKFLUG"].strip()
    if env.get("TERMINE_PRO_LAUF", "").strip():
        cfg["budget"]["termine_pro_lauf"] = int(env["TERMINE_PRO_LAUF"])
    return cfg


def erzeuge_termine(cfg):
    r = cfg["reise"]
    start = date.fromisoformat(r["fruehester_abflug"])
    ende = date.fromisoformat(r["spaetester_rueckflug"])
    versatz = 1 if r.get("nachtflug", True) else 0
    schritt = max(1, int(r.get("schrittweite_tage", 3)))
    heute = date.today()
    termine = []
    for n in r["naechte"]:
        abflug = start
        while True:
            checkin = abflug + timedelta(days=versatz)
            checkout = checkin + timedelta(days=n)
            if checkout > ende:
                break
            if abflug > heute:
                termine.append({
                    "id": f"{abflug}_{checkout}",
                    "abflug": abflug.isoformat(), "rueckflug": checkout.isoformat(),
                    "checkin": checkin.isoformat(), "checkout": checkout.isoformat(),
                    "naechte": n,
                })
            abflug += timedelta(days=schritt)
    return termine


def waehle_termine(cfg, termine, status):
    """Rotiert durch alle Termine, damit das Suchkontingent über die Tage verteilt wird."""
    k = max(1, int(cfg["budget"]["termine_pro_lauf"]))
    signatur = hashlib.sha1(
        json.dumps([cfg["reise"], cfg["abflughaefen"]], sort_keys=True).encode()
    ).hexdigest()[:12]
    rotation = status.setdefault("rotation", {})
    i = rotation.get(signatur, 0) % len(termine)
    auswahl = [termine[(i + j) % len(termine)] for j in range(min(k, len(termine)))]
    rotation[signatur] = (i + len(auswahl)) % len(termine)
    return auswahl


# ----------------------------------------------------------------- SerpApi
def serpapi(params):
    params = {**params, "api_key": os.environ["SERPAPI_KEY"], "hl": "de", "gl": "de"}
    antwort = requests.get(SERPAPI_URL, params=params, timeout=120)
    daten = antwort.json()
    if daten.get("error"):
        raise RuntimeError(daten["error"])
    antwort.raise_for_status()
    return daten


def suche_fluege(cfg, von, t):
    """Günstigster Hin- und Rückflug, der zu den Wunsch-Flugzeiten passt."""
    r, fz = cfg["reise"], cfg.get("flugzeiten", {})
    hin_bis = uhrzeit(fz.get("hinflug_ankunft_male_bis", "23:59"))
    rueck_bis = uhrzeit(fz.get("rueckflug_ankunft_deutschland_bis", "23:59"))
    params = {
        "engine": "google_flights",
        "departure_id": von, "arrival_id": cfg["zielflughafen"],
        "outbound_date": t["abflug"], "return_date": t["rueckflug"],
        "type": "1", "adults": r["erwachsene"],
        "travel_class": KLASSEN.get(r.get("reiseklasse", "economy"), 1),
        "currency": r.get("waehrung", "EUR"),
        # Vorfilter bei Google: Abflug egal (0–23 Uhr), Ankunft bis zur gewünschten Stunde
        "outbound_times": f"0,23,0,{hin_bis[0]}",
        "return_times": f"0,23,0,{rueck_bis[0]}",
    }
    res = serpapi(params)
    link = res.get("search_metadata", {}).get("google_flights_url")

    # Feinprüfung Hinflug: Landung in Malé am ersten Urlaubstag bis zur Wunschzeit
    hin = [a for a in (res.get("best_flights") or []) + (res.get("other_flights") or [])
           if isinstance(a.get("price"), (int, float))
           and ankunft_ok(a.get("flights"), t["checkin"], hin_bis)]
    if not hin:
        return None
    best = min(hin, key=lambda a: a["price"])
    hs = best.get("flights") or []
    ergebnis = {
        "preis": float(best["price"]),
        "airlines": sorted({s.get("airline", "?") for s in hs}),
        "route": route_von(hs),
        "hin_abflug": hhmm(hs[0], "departure_airport"),
        "hin_ankunft": hhmm(hs[-1], "arrival_airport"),
        "dauer_h": round(best["total_duration"] / 60, 1) if best.get("total_duration") else None,
        "rueck_abflug": None, "rueck_ankunft": None, "rueck_route": None,
        "link": link,
    }

    # Feinprüfung Rückflug: Ankunft in Deutschland am Abreisetag bis zur Wunschzeit
    if fz.get("rueckflug_details_laden", True) and best.get("departure_token"):
        res2 = serpapi({**params, "departure_token": best["departure_token"]})
        rueck = [a for a in (res2.get("best_flights") or []) + (res2.get("other_flights") or [])
                 if isinstance(a.get("price"), (int, float))
                 and ankunft_ok(a.get("flights"), t["rueckflug"], rueck_bis)]
        if not rueck:
            return {"ohne_passenden_rueckflug": True}
        rb = min(rueck, key=lambda a: a["price"])
        rs = rb.get("flights") or []
        ergebnis.update({
            "preis": float(rb["price"]),
            "airlines": sorted(set(ergebnis["airlines"]) | {s.get("airline", "?") for s in rs}),
            "rueck_abflug": hhmm(rs[0], "departure_airport"),
            "rueck_ankunft": hhmm(rs[-1], "arrival_airport"),
            "rueck_route": route_von(rs),
            "link": res2.get("search_metadata", {}).get("google_flights_url") or link,
        })
    return ergebnis


def passt(hotelname, kandidat):
    woerter = [w for w in hotelname.lower().split() if len(w) > 2]
    return all(w in kandidat.lower() for w in woerter)


def suche_hotel(cfg, hotel, t):
    r = cfg["reise"]
    res = serpapi({
        "engine": "google_hotels", "q": hotel["suchbegriff"],
        "check_in_date": t["checkin"], "check_out_date": t["checkout"],
        "adults": r["erwachsene"], "currency": r.get("waehrung", "EUR"),
    })
    if "properties" in res:  # Ergebnisliste → passendes Hotel heraussuchen
        objekt = next((p for p in res["properties"] if passt(hotel["name"], p.get("name", ""))), None)
    else:                    # eindeutiger Treffer → direkte Hoteldetails
        objekt = res
    if not objekt:
        return None

    naechte = t["naechte"]
    ab = betrag(objekt.get("total_rate"))
    if ab is None and betrag(objekt.get("rate_per_night")):
        ab = betrag(objekt.get("rate_per_night")) * naechte

    zimmer_preis = zimmer_name = zimmer_quelle = None
    stichwort = (hotel.get("zimmer_stichwort") or "").lower()
    if stichwort:
        for quelle in (objekt.get("featured_prices") or []) + (objekt.get("prices") or []):
            for z in quelle.get("rooms") or []:
                name = z.get("name") or ""
                if stichwort not in name.lower():
                    continue
                p = betrag(z.get("total_rate"))
                if p is None and betrag(z.get("rate_per_night")):
                    p = betrag(z.get("rate_per_night")) * naechte
                if p and (zimmer_preis is None or p < zimmer_preis):
                    zimmer_preis, zimmer_name, zimmer_quelle = p, name, quelle.get("source")

    if ab is None and zimmer_preis is None:
        return None
    return {
        "preis_ab": ab, "zimmer_preis": zimmer_preis, "zimmer_name": zimmer_name,
        "zimmer_quelle": zimmer_quelle,
        "link": objekt.get("link") or res.get("search_metadata", {}).get("google_hotels_url"),
    }


# ------------------------------------------------------- Claude-Websuche
def claude_angebote(cfg):
    r, cw = cfg["reise"], cfg["claude_websuche"]
    flughaefen = ", ".join(fh(c) for c in cfg["abflughaefen"])
    naechte = ", ".join(str(n) for n in r["naechte"])
    prompt = f"""Suche im Web nach aktuell buchbaren Angeboten (Pauschalreise oder Flug + Hotel)
für einen Malediven-Urlaub:
- Abflug ab {flughaefen} (Abflug ab anderen deutschen Flughäfen nur nennen, wenn deutlich günstiger)
- Reisezeitraum zwischen {de(r['fruehester_abflug'])} und {de(r['spaetester_rueckflug'])}
- {naechte} Nächte, {r['erwachsene']} Erwachsene
- Traumziel: Water Villa / Grand Water Villa im Sun Siyam Olhuveli (South Male Atoll).
  Vergleichbare Resorts mit Überwasser-Villen sind ebenfalls interessant.
Bevorzuge deutsche Reiseportale, Veranstalter und Schnäppchenseiten.

Antworte AUSSCHLIESSLICH mit JSON ohne weiteren Text, in diesem Format:
{{"angebote": [{{"titel": "...", "resort": "...", "zimmer": "...", "zeitraum": "...",
"abflughafen": "...", "preis_gesamt_eur": 1234, "verpflegung": "...", "quelle": "...", "url": "https://..."}}]}}
Höchstens 8 Angebote, nur echte Fundstellen mit URL; unbekannte Werte als null."""

    antwort = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                 "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={
            "model": cw.get("modell", "claude-sonnet-5-5"),
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"type": "web_search_20250305", "name": "web_search",
                       "max_uses": int(cw.get("max_websuchen", 8))}],
        },
        timeout=600,
    )
    antwort.raise_for_status()
    text = "".join(b.get("text", "") for b in antwort.json().get("content", [])
                   if b.get("type") == "text")
    start, ende = text.find("{"), text.rfind("}")
    if start < 0 or ende < 0:
        return []
    try:
        return json.loads(text[start:ende + 1]).get("angebote", [])
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- E-Mail
def email_empfaenger(cfg):
    roh = os.environ.get("EMAIL_EMPFAENGER", "").strip()
    if roh:
        liste = roh.replace(";", ",").split(",")
    else:
        liste = cfg.get("benachrichtigung", {}).get("email_empfaenger") or []
    return [a.strip() for a in liste if a and "@" in a]


def sende_email(cfg, betreff, text_md):
    empfaenger = email_empfaenger(cfg)
    server = os.environ.get("SMTP_SERVER", "").strip()
    benutzer = os.environ.get("SMTP_BENUTZER", "").strip()
    passwort = os.environ.get("SMTP_PASSWORT", "")
    if not (empfaenger and server and benutzer and passwort):
        print("E-Mail übersprungen: Empfänger oder SMTP-Zugangsdaten fehlen (siehe README).")
        return False
    port = int(os.environ.get("SMTP_PORT", "").strip() or 587)

    nachricht = EmailMessage()
    nachricht["Subject"] = betreff
    nachricht["From"] = os.environ.get("EMAIL_ABSENDER", "").strip() or benutzer
    nachricht["To"] = ", ".join(empfaenger)
    nachricht.set_content(text_md)
    html = markdown.markdown(text_md, extensions=["tables"])
    nachricht.add_alternative(
        f"<html><body style='font-family:Arial,sans-serif'>{html}</body></html>", subtype="html")

    if port == 465:
        with smtplib.SMTP_SSL(server, port, timeout=60) as smtp:
            smtp.login(benutzer, passwort)
            smtp.send_message(nachricht)
    else:
        with smtplib.SMTP(server, port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(benutzer, passwort)
            smtp.send_message(nachricht)
    print(f"E-Mail gesendet an {len(empfaenger)} Empfänger.")
    return True


def report_link():
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return ""
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/blob/HEAD/REPORT.md"


# ------------------------------------------------------------- Auswertung
def neuester_stand(verlauf):
    stand = {}
    for e in verlauf:  # chronologisch → der letzte gewinnt
        stand[e["schluessel"]] = e
    return stand


def hotelpreis(e):
    return e.get("zimmer_preis") or e.get("preis_ab")


def schreibe_report(cfg, verlauf, status, termine):
    stand = neuester_stand(verlauf)
    gueltig = {t["id"] for t in termine}
    fluege = [e for e in stand.values() if e["typ"] == "flug" and e["termin"] in gueltig
              and e["von"] in cfg["abflughaefen"]]
    hotels = [e for e in stand.values() if e["typ"] == "hotel" and e["termin"] in gueltig]
    r = cfg["reise"]
    geprueft = {e["termin"] for e in fluege}

    z = [f"# 🏝️ Malediven-Angebote",
         f"",
         f"**Stand:** {datetime.now().strftime('%d.%m.%Y %H:%M')} UTC  ",
         f"**Zeitraum:** {de(r['fruehester_abflug'])} – {de(r['spaetester_rueckflug'])} · "
         f"**Nächte:** {', '.join(map(str, r['naechte']))} · **Reisende:** {r['erwachsene']} · "
         f"**Ab:** {', '.join(fh(c) for c in cfg['abflughaefen'])}  ",
         f"**Flugzeiten:** Ankunft Malé bis {cfg.get('flugzeiten', {}).get('hinflug_ankunft_male_bis', '–')} Uhr "
         f"am 1. Urlaubstag · zurück in Deutschland bis "
         f"{cfg.get('flugzeiten', {}).get('rueckflug_ankunft_deutschland_bis', '–')} Uhr  ",
         f"**Abdeckung:** {len(geprueft)} von {len(termine)} Terminen bereits geprüft",
         ""]

    # Pakete (Flug + Hotel am selben Termin)
    pakete = []
    for h in hotels:
        hp = hotelpreis(h)
        flug = [f for f in fluege if f["termin"] == h["termin"]]
        if hp and flug:
            best = min(flug, key=lambda f: f["preis"])
            pakete.append((best["preis"] + hp, h, best))
    pakete.sort(key=lambda p: p[0])
    z += ["## Beste Kombinationen Flug + Hotel", ""]
    if pakete:
        z += ["| Summe | Hotel | Zimmer | Termin | Nächte | Ab | Flug | Hotel |",
              "|---:|---|---|---|---:|---|---:|---:|"]
        for summe, h, f in pakete[:15]:
            zimmer = h.get("zimmer_name") or "günstigstes Zimmer"
            z.append(f"| **{euro(summe)}** | [{h['hotel']}]({h.get('link') or ''}) | {zimmer} | "
                     f"{de(f['abflug'])} – {de(f['rueckflug'])} | {h['naechte']} | {f['von']} | "
                     f"[{euro(f['preis'])}]({f.get('link') or ''}) | {euro(hotelpreis(h))} |")
    else:
        z.append("_Noch keine Kombinationen – die Daten füllen sich mit jedem Lauf._")
    z.append("")

    # Flüge je Abflughafen
    z += ["## Günstigste Flüge je Abflughafen (Hin & Zurück, alle Reisenden)", ""]
    for code in cfg["abflughaefen"]:
        eigene = sorted([f for f in fluege if f["von"] == code], key=lambda f: f["preis"])[:5]
        z.append(f"### {fh(code)}")
        if not eigene:
            z += ["_noch keine Daten_", ""]
            continue
        z += ["| Preis | Hinflug | Ankunft Malé | Rückflug | Ankunft DE | Route hin / zurück | Airline(s) |",
              "|---:|---|---|---|---|---|---|"]
        for f in eigene:
            z.append(f"| [{euro(f['preis'])}]({f.get('link') or ''}) | "
                     f"{de(f['abflug'])} {f.get('hin_abflug') or ''} | {f.get('hin_ankunft') or '–'} | "
                     f"{de(f['rueckflug'])} {f.get('rueck_abflug') or ''} | {f.get('rueck_ankunft') or '–'} | "
                     f"{f.get('route', '')} / {f.get('rueck_route') or '–'} | {', '.join(f.get('airlines', []))} |")
        z.append("")

    # Web-Angebote
    web = status.get("web_angebote")
    z += ["## Pauschalangebote aus der Websuche (Claude)", ""]
    if web and web.get("angebote"):
        z.append(f"_gefunden am {web['zeit'][:10]} – Preise und Verfügbarkeit bitte auf der Seite prüfen_")
        z.append("")
        for a in web["angebote"]:
            preis = euro(a["preis_gesamt_eur"]) if isinstance(a.get("preis_gesamt_eur"), (int, float)) else "Preis k. A."
            z.append(f"- **{a.get('titel') or a.get('resort')}** – {preis} · {a.get('zimmer') or ''} · "
                     f"{a.get('zeitraum') or ''} · ab {a.get('abflughafen') or '?'} · "
                     f"[{a.get('quelle') or 'Link'}]({a.get('url') or ''})")
    else:
        z.append("_deaktiviert oder noch keine Ergebnisse_")
    z.append("")
    REPORT.write_text("\n".join(z), encoding="utf-8")


# ------------------------------------------------------------------ Ablauf
def main():
    cfg = lade_config()
    verlauf = lade_json(VERLAUF, [])
    status = lade_json(STATUS, {})
    stand = neuester_stand(verlauf)
    termine = erzeuge_termine(cfg)
    alarme = []
    a_cfg = cfg.get("alarm", {})

    # bisherige Tiefstpreise je Abflughafen
    tief = {}
    for e in verlauf:
        if e["typ"] == "flug":
            tief[e["von"]] = min(tief.get(e["von"], e["preis"]), e["preis"])

    if not termine:
        print("Keine zukünftigen Termine mehr – der Suchzeitraum ist vorbei oder leer.")
    elif not os.environ.get("SERPAPI_KEY"):
        print("WARNUNG: Kein SERPAPI_KEY gesetzt – Flug- und Hotelsuche übersprungen.")
    else:
        aktive_hotels = [h for h in cfg.get("hotels", []) if h.get("aktiv")]
        auswahl = waehle_termine(cfg, termine, status)
        print(f"{len(termine)} Termine insgesamt, prüfe heute {len(auswahl)} "
              f"(= {len(auswahl) * (len(cfg['abflughaefen']) + len(aktive_hotels))} Suchen)")

        for t in auswahl:
            print(f"\n▶ Termin {de(t['abflug'])} – {de(t['rueckflug'])} ({t['naechte']} Nächte)")
            flug_preise = []
            for von in cfg["abflughaefen"]:
                try:
                    f = suche_fluege(cfg, von, t)
                except Exception as ex:
                    print(f"  ✗ Flug {von}: {ex}")
                    continue
                if not f:
                    print(f"  – Flug {von}: kein Hinflug mit passender Ankunftszeit")
                    continue
                if f.get("ohne_passenden_rueckflug"):
                    print(f"  – Flug {von}: kein Rückflug mit Ankunft bis zur Wunschzeit")
                    continue
                key = f"flug|{von}|{t['id']}"
                e = {"schluessel": key, "typ": "flug", "zeit": jetzt(), "von": von,
                     "termin": t["id"], "abflug": t["abflug"], "rueckflug": t["rueckflug"],
                     "naechte": t["naechte"], **f}
                print(f"  ✓ Flug {von}: {euro(f['preis'])} ({f['route']}, an {f['hin_ankunft']} / "
                      f"zurück an {f['rueck_ankunft'] or '?'})")
                geaendert = key not in stand or stand[key]["preis"] != f["preis"]
                if geaendert and f["preis"] <= a_cfg.get("max_preis_flug_gesamt", 0):
                    alarme.append(f"✈️ Flug ab {fh(von)} für **{euro(f['preis'])}** "
                                  f"({de(t['abflug'])} – {de(t['rueckflug'])}) – [ansehen]({f['link']})")
                elif geaendert and a_cfg.get("bei_neuem_tiefstpreis") and von in tief and f["preis"] < tief[von]:
                    alarme.append(f"📉 Neuer Tiefstpreis ab {fh(von)}: **{euro(f['preis'])}** "
                                  f"(vorher {euro(tief[von])}) für {de(t['abflug'])} – "
                                  f"{de(t['rueckflug'])} – [ansehen]({f['link']})")
                tief[von] = min(tief.get(von, f["preis"]), f["preis"])
                verlauf.append(e)
                flug_preise.append((f["preis"], von))

            for h in aktive_hotels:
                try:
                    hp = suche_hotel(cfg, h, t)
                except Exception as ex:
                    print(f"  ✗ Hotel {h['name']}: {ex}")
                    continue
                if not hp:
                    print(f"  – Hotel {h['name']}: kein Preis gefunden")
                    continue
                key = f"hotel|{h['name']}|{t['id']}"
                e = {"schluessel": key, "typ": "hotel", "zeit": jetzt(), "hotel": h["name"],
                     "termin": t["id"], "naechte": t["naechte"], **hp}
                print(f"  ✓ {h['name']}: ab {euro(hp['preis_ab'])}, "
                      f"{hp['zimmer_name'] or h.get('zimmer_stichwort')}: {euro(hp['zimmer_preis'])}")
                preis = hotelpreis(e)
                if flug_preise and preis:
                    fp, von = min(flug_preise)
                    vorher = stand.get(key)
                    if (fp + preis) <= a_cfg.get("max_preis_paket_gesamt", 0) and \
                            (not vorher or hotelpreis(vorher) != preis):
                        alarme.append(f"🏝️ {h['name']} ({hp['zimmer_name'] or 'Zimmer'}) + Flug ab "
                                      f"{fh(von)}: **{euro(fp + preis)}** für {t['naechte']} Nächte "
                                      f"({de(t['checkin'])} – {de(t['checkout'])}) – [Hotel]({hp['link']})")
                verlauf.append(e)

    if cfg.get("claude_websuche", {}).get("aktiv"):
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                angebote = claude_angebote(cfg)
                status["web_angebote"] = {"zeit": jetzt(), "angebote": angebote}
                print(f"\nClaude-Websuche: {len(angebote)} Angebote gefunden")
            except Exception as ex:
                print(f"\n✗ Claude-Websuche fehlgeschlagen: {ex}")
        else:
            print("\nHinweis: Claude-Websuche aktiv, aber kein ANTHROPIC_API_KEY gesetzt.")

    speichere_json(VERLAUF, verlauf[-20000:])
    status["letzter_lauf"] = jetzt()
    speichere_json(STATUS, status)
    schreibe_report(cfg, verlauf, status, termine)

    b = cfg.get("benachrichtigung", {})
    alarm_md = ""
    if alarme:
        alarm_md = ("## 🔔 Neue Treffer\n\n" + "\n".join(f"- {a}" for a in alarme)
                    + f"\n\nAlle Ergebnisse: {report_link() or 'REPORT.md im Repository'}\n")
        print(f"\n🔔 {len(alarme)} Alarm(e)")
        if b.get("github_issue"):
            ALARM.write_text(alarm_md, encoding="utf-8")

    heute = datetime.now().strftime("%d.%m.%Y")
    try:
        if os.environ.get("EMAIL_TEST", "").lower() == "true":
            sende_email(cfg, f"🏝️ Testmail Malediven-Suche ({heute})",
                        "Die E-Mail-Benachrichtigung funktioniert. 🎉\n\n" + REPORT.read_text(encoding="utf-8"))
        elif b.get("taeglicher_bericht"):
            titel = "Preisalarm + Tagesbericht" if alarme else "Tagesbericht"
            sende_email(cfg, f"🏝️ Malediven {titel} {heute}",
                        (alarm_md + "\n\n" if alarm_md else "") + REPORT.read_text(encoding="utf-8"))
        elif alarme and b.get("bei_alarm", True):
            sende_email(cfg, f"🏝️ Malediven-Preisalarm {heute}", alarm_md)
    except Exception as ex:
        print(f"✗ E-Mail-Versand fehlgeschlagen: {ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
