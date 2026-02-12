#!/usr/bin/env python3
"""
Flight Monitor — automatic flight price alerts via Google Flights.
No API key needed. Uses Playwright for scraping.
"""

import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import time
from datetime import datetime, timedelta, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

# Aggiungi directory dello script al path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from fast_flights import FlightData, Passengers

try:
    from scraper import search_flights, build_url
except ImportError:
    print("Errore: scraper.py non trovato nella directory dello script")
    sys.exit(1)

try:
    import requests
except ImportError:
    requests = None

CONFIG_PATH = SCRIPT_DIR / "config.json"
EXAMPLE_CONFIG_PATH = SCRIPT_DIR / "config.example.json"
HISTORY_PATH = SCRIPT_DIR / "price_history.jsonl"
LOG_PATH = SCRIPT_DIR / "monitor.log"
LAST_ALERT_PATH = SCRIPT_DIR / ".last_alert"

# Nomi aeroporti noti (estendibili)
KNOWN_AIRPORTS = {
    "MXP": "Malpensa",
    "LIN": "Linate",
    "BGY": "Bergamo",
    "MLE": "Malé",
    "FCO": "Fiumicino",
    "VCE": "Venezia",
    "BLQ": "Bologna",
    "NAP": "Napoli",
    "PMO": "Palermo",
    "CTA": "Catania",
    "TRN": "Torino",
    "FLR": "Firenze",
    "PSA": "Pisa",
}

# Google Flights mostra risultati ~330 giorni in avanti
MAX_DAYS_AHEAD = 330


def load_config():
    """Carica config.json, creandolo da config.example.json se mancante.
    Le variabili d'ambiente hanno priorità sui valori nel file."""
    if not CONFIG_PATH.exists():
        if EXAMPLE_CONFIG_PATH.exists():
            shutil.copy(EXAMPLE_CONFIG_PATH, CONFIG_PATH)
            print(f"Creato {CONFIG_PATH} da {EXAMPLE_CONFIG_PATH}")
            print("Modifica config.json con i tuoi dati prima di proseguire.")
            sys.exit(0)
        else:
            print("Errore: config.json non trovato e config.example.json mancante")
            sys.exit(1)

    with open(CONFIG_PATH) as f:
        config = json.load(f)

    # Override da variabili d'ambiente
    env_overrides = {
        "FLIGHT_EMAIL_TO": "email_to",
        "FLIGHT_EMAIL_FROM": "email_from",
        "FLIGHT_EMAIL_CC": "email_cc",
        "FLIGHT_EMAIL_PASSWORD": "email_app_password",
        "FLIGHT_TELEGRAM_TOKEN": "telegram_bot_token",
        "FLIGHT_TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_var, config_key in env_overrides.items():
        val = os.environ.get(env_var)
        if val:
            config[config_key] = val

    return config


def get_airport_name(code):
    """Restituisce il nome dell'aeroporto o il codice IATA se sconosciuto."""
    return KNOWN_AIRPORTS.get(code, code)


def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    with open(LOG_PATH, "a") as f:
        f.write(line + "\n")


def parse_price(price_str):
    """Estrae il valore numerico da una stringa prezzo (es. '€1.234', '€1,234')."""
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d.,]", "", str(price_str))
    if "," in cleaned and "." in cleaned:
        if cleaned.rindex(",") > cleaned.rindex("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def generate_date_pairs(config):
    """Genera coppie (partenza, ritorno, notti) campionando il periodo disponibile."""
    start = datetime.strptime(config["date_from"], "%Y-%m-%d").date()
    end = datetime.strptime(config["date_to"], "%Y-%m-%d").date()
    today = date.today()
    max_date = today + timedelta(days=MAX_DAYS_AHEAD)

    # Restringi al periodo effettivamente disponibile su Google Flights
    effective_start = max(start, today + timedelta(days=1))
    effective_end = min(end, max_date)

    if effective_start > effective_end:
        return [], start, max_date

    step = config.get("sample_every_n_days", 5)
    nights_options = [config["nights_min"], config["nights_max"]]
    if config["nights_max"] - config["nights_min"] > 2:
        mid = (config["nights_min"] + config["nights_max"]) // 2
        nights_options.insert(1, mid)

    pairs = []
    current = effective_start
    while current <= effective_end:
        for nights in nights_options:
            ret = current + timedelta(days=nights)
            if ret <= max_date:
                pairs.append((
                    current.strftime("%Y-%m-%d"),
                    ret.strftime("%Y-%m-%d"),
                    nights,
                ))
        current += timedelta(days=step)
    return pairs, start, max_date


def parse_stops(stops_str):
    """Parsa il numero di scali da stringa."""
    if not stops_str:
        return 0
    s = stops_str.lower()
    if "nonstop" in s or "dirett" in s:
        return 0
    match = re.search(r"(\d+)", s)
    return int(match.group(1)) if match else 0


def run_search(flight_data, trip, adults, currency="EUR"):
    """Esegue una singola ricerca volo."""
    try:
        result = search_flights(
            flight_data=flight_data,
            trip=trip,
            passengers=Passengers(adults=adults),
            currency=currency,
            timeout_ms=45000,
        )
        return result
    except Exception as e:
        log(f"    Errore: {e}")
        return None


def search_return_flights(destination, origin, ret_date, adults, config):
    """Cerca dettagli voli di ritorno (one-way MLE→origin)."""
    result = run_search(
        flight_data=[FlightData(date=ret_date, from_airport=destination, to_airport=origin)],
        trip="one-way",
        adults=adults,
    )
    if not result or not result.flights:
        return None

    # Prendi il miglior volo di ritorno con max 1 scalo
    for f in result.flights:
        num_stops = parse_stops(f.stops)
        if num_stops <= config["max_stops"]:
            return {
                "ret_airline": f.airline,
                "ret_departure": f.departure,
                "ret_arrival": f.arrival,
                "ret_duration": f.duration,
                "ret_stops": num_stops,
                "ret_stops_detail": f.stops or "Diretto",
            }
    return None


def process_results(result, origin, dep_date, ret_date, nights, config):
    """Analizza i risultati di una ricerca e filtra."""
    flights = []
    if not result or not result.flights:
        return flights

    for f in result.flights:
        price = parse_price(f.price)
        if price is None:
            continue

        price_pp = price / config["adults"]
        num_stops = parse_stops(f.stops)

        if num_stops > config["max_stops"]:
            continue

        # Link diretto a Google Flights per questa ricerca
        flights_url = build_url(
            flight_data=[
                FlightData(date=dep_date, from_airport=origin, to_airport=config["destination"]),
                FlightData(date=ret_date, from_airport=config["destination"], to_airport=origin),
            ],
            trip="round-trip",
            passengers=Passengers(adults=config["adults"]),
            currency="EUR",
        )

        flights.append({
            "price_total": price,
            "price_pp": round(price_pp, 2),
            "dep_date": datetime.strptime(dep_date, "%Y-%m-%d").strftime("%d/%m/%Y"),
            "ret_date": datetime.strptime(ret_date, "%Y-%m-%d").strftime("%d/%m/%Y"),
            "dep_airport": get_airport_name(origin),
            "dest_airport": get_airport_name(config["destination"]),
            "origin_code": origin,
            "airline": f.airline,
            "departure": f.departure,
            "arrival": f.arrival,
            "duration": f.duration,
            "stops": num_stops,
            "stops_detail": f.stops or "Diretto",
            "nights": nights,
            "link": flights_url,
        })

    return flights


def save_history(results):
    timestamp = datetime.now().isoformat()
    with open(HISTORY_PATH, "a") as f:
        for r in results[:10]:
            entry = {"timestamp": timestamp, **r}
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send_macos_notification(title, message):
    escaped_msg = message.replace('"', '\\"').replace("'", "\\'")
    escaped_title = title.replace('"', '\\"')
    script = f'display notification "{escaped_msg}" with title "{escaped_title}" sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def send_telegram(config, message):
    if not requests:
        return
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        log("Notifica Telegram inviata")
    except Exception as e:
        log(f"Errore Telegram: {e}")


def _route_label(config):
    """Genera etichetta rotta (es. 'Milano - Maldive')."""
    origin_names = sorted(set(get_airport_name(o) for o in config.get("origins", [])))
    dest_name = get_airport_name(config.get("destination", ""))
    origins_str = ", ".join(origin_names) if len(origin_names) <= 3 else f"{len(origin_names)} aeroporti"
    return f"{origins_str} - {dest_name}"


def send_heartbeat_email(config, best_price, total_flights):
    """Invia email settimanale di conferma funzionamento (nessuna offerta trovata)."""
    email_to = config.get("email_to", "")
    email_from = config.get("email_from", "")
    email_cc = config.get("email_cc", "")
    app_password = config.get("email_app_password", "")
    if not email_to or not app_password or app_password == "YOUR_APP_PASSWORD":
        return

    route = _route_label(config)
    threshold = config["price_threshold_pp"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Flight Monitor attivo — nessuna offerta sotto €{threshold}/pp questa settimana"
    msg["From"] = email_from
    msg["To"] = email_to
    if email_cc:
        msg["Cc"] = email_cc

    text_body = (
        f"Ciao,\n\n"
        f"Flight Monitor per la rotta {route} è attivo e funzionante.\n\n"
        f"Questa settimana non sono stati trovati voli sotto la soglia di €{threshold}/persona.\n"
        f"Miglior prezzo trovato nell'ultimo check: €{best_price:.0f}/persona "
        f"({total_flights} voli analizzati).\n\n"
        f"Continuo a monitorare ogni {config.get('check_interval_hours', 12)} ore.\n\n"
        f"-- Flight Monitor {route}"
    )

    msg.attach(MIMEText(text_body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_from, app_password)
            recipients = [email_to] + ([email_cc] if email_cc else [])
            server.sendmail(email_from, recipients, msg.as_string())
        log("Email heartbeat settimanale inviata")
    except Exception as e:
        log(f"Errore invio email heartbeat: {e}")


def send_email(config, flights, threshold):
    """Invia email con le offerte trovate via Gmail SMTP."""
    email_to = config.get("email_to", "")
    email_from = config.get("email_from", "")
    email_cc = config.get("email_cc", "")
    app_password = config.get("email_app_password", "")
    if not email_to or not app_password or app_password == "YOUR_APP_PASSWORD":
        return

    route = _route_label(config)
    dest_name = get_airport_name(config.get("destination", ""))

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Volo {route} da €{flights[0]['price_pp']:.0f}/persona!"
    msg["From"] = email_from
    msg["To"] = email_to
    if email_cc:
        msg["Cc"] = email_cc

    # Versione testo
    text_lines = [
        f"Trovati {len(flights)} voli A/R sotto €{threshold}/persona!\n",
        f"Prezzi per {config['adults']} adulti, andata e ritorno.\n",
    ]
    for i, f in enumerate(flights[:10], 1):
        out_stops = "Diretto" if f["stops"] == 0 else f"{f['stops']} scalo"
        ret_stops = "Diretto" if f.get("ret_stops", 0) == 0 else f"{f.get('ret_stops', '?')} scalo"
        text_lines.append(
            f"#{i} €{f['price_pp']:.0f}/pp A/R (€{f['price_total']:.0f} totale per {config['adults']})\n"
            f"   ANDATA  {f['dep_date']}  {f['dep_airport']} → {dest_name}\n"
            f"           {f['airline']} | {f['duration']} | {out_stops}\n"
            f"   RITORNO {f['ret_date']}  {dest_name} → {f['dep_airport']}\n"
            f"           {f.get('ret_airline', '?')} | {f.get('ret_duration', '?')} | {ret_stops}\n"
            f"   {f['nights']} notti\n"
            f"   {f.get('link', '')}\n"
        )
    text_lines.append(f"\n-- Flight Monitor {route}")
    text_body = "\n".join(text_lines)

    # Versione HTML
    html_rows = ""
    for i, f in enumerate(flights[:10], 1):
        out_stops = "Diretto" if f["stops"] == 0 else f"{f['stops']} scalo"
        ret_stops = "Diretto" if f.get("ret_stops", 0) == 0 else f"{f.get('ret_stops', '?')} scalo"
        link = f.get("link", "")
        html_rows += f"""
        <tr style="border-bottom:1px solid #eee;">
            <td style="padding:14px;text-align:center;vertical-align:top;">
                <div style="font-size:28px;font-weight:bold;color:#2e7d32;">€{f['price_pp']:.0f}</div>
                <div style="font-size:11px;color:#888;">/persona A/R</div>
                <div style="font-size:11px;color:#aaa;margin-top:2px;">€{f['price_total']:.0f} per 2</div>
            </td>
            <td style="padding:14px;">
                <div style="margin-bottom:8px;padding:8px;background:#f8f9fa;border-radius:6px;">
                    <div style="color:#1a73e8;font-weight:bold;font-size:12px;margin-bottom:3px;">✈ ANDATA — {f['dep_date']}</div>
                    <div style="color:#333;font-weight:bold;">{f['dep_airport']} → {dest_name}</div>
                    <div style="color:#666;font-size:13px;">{f['airline']} | {f['duration']} | {out_stops}</div>
                </div>
                <div style="margin-bottom:8px;padding:8px;background:#f8f9fa;border-radius:6px;">
                    <div style="color:#1a73e8;font-weight:bold;font-size:12px;margin-bottom:3px;">✈ RITORNO — {f['ret_date']}</div>
                    <div style="color:#333;font-weight:bold;">{dest_name} → {f['dep_airport']}</div>
                    <div style="color:#666;font-size:13px;">{f.get('ret_airline', '?')} | {f.get('ret_duration', '?')} | {ret_stops}</div>
                </div>
                <div style="font-size:12px;color:#888;margin-bottom:8px;">{f['nights']} notti</div>
                <a href="{link}" style="display:inline-block;background:#1a73e8;color:white;padding:6px 14px;border-radius:4px;text-decoration:none;font-size:13px;">Vedi e prenota su Google Flights →</a>
            </td>
        </tr>"""

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
        <div style="background:#1a73e8;color:white;padding:20px;border-radius:8px 8px 0 0;">
            <h2 style="margin:0;">✈ Voli {route}</h2>
            <p style="margin:5px 0 0;opacity:0.9;">{len(flights)} voli andata e ritorno sotto €{threshold}/persona</p>
            <p style="margin:3px 0 0;opacity:0.7;font-size:13px;">Prezzi totali A/R per {config['adults']} adulti</p>
        </div>
        <table style="width:100%;border-collapse:collapse;">{html_rows}</table>
        <div style="padding:15px;color:#888;font-size:12px;">
            Flight Monitor {route}
        </div>
    </body></html>"""

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(email_from, app_password)
            recipients = [email_to] + ([email_cc] if email_cc else [])
            server.sendmail(email_from, recipients, msg.as_string())
        log(f"Email inviata a {email_to}" + (f" (cc: {email_cc})" if email_cc else ""))
    except Exception as e:
        log(f"Errore invio email: {e}")


def format_flight_text(f, index, config):
    out_stops = "Diretto" if f["stops"] == 0 else f"{f['stops']} scalo"
    ret_stops = "Diretto" if f.get("ret_stops", 0) == 0 else f"{f.get('ret_stops', '?')} scalo"
    dest_name = get_airport_name(config.get("destination", ""))
    return "\n".join([
        f"{'='*55}",
        f"  VOLO #{index} — €{f['price_pp']:.0f}/pp A/R (€{f['price_total']:.0f} tot per {config['adults']}, {f['nights']}n)",
        f"{'='*55}",
        f"  ANDATA   {f['dep_date']}  {f['dep_airport']} → {dest_name}",
        f"           {f['airline']} | {f['duration']} | {out_stops}",
        f"  RITORNO  {f['ret_date']}  {dest_name} → {f['dep_airport']}",
        f"           {f.get('ret_airline', '?')} | {f.get('ret_duration', '?')} | {ret_stops}",
        f"  Link:    {f.get('link', 'N/A')}",
    ])


def format_telegram_message(flights, threshold, config):
    route = _route_label(config)
    lines = [f"<b>Voli {route} sotto €{threshold}/pp!</b>\n"]
    for i, f in enumerate(flights[:5], 1):
        stops_str = "Diretto" if f["stops"] == 0 else f"{f['stops']} scalo"
        lines.append(
            f"<b>#{i} €{f['price_pp']:.0f}/pp (€{f['price_total']:.0f} tot)</b>\n"
            f"{f['dep_date']}-{f['ret_date']} ({f['nights']}n)\n"
            f"Da: {f['dep_airport']} | {f['airline']}\n"
            f"{f['duration']} | {stops_str}\n"
        )
    return "\n".join(lines)


def main():
    log("=" * 60)
    config = load_config()
    route = _route_label(config)
    log(f"Avvio monitoraggio voli {route}")
    log("Fonte: Google Flights (Playwright)")

    threshold = config["price_threshold_pp"]
    delay = config.get("delay_between_searches", 4)

    log(f"Soglia: €{threshold}/persona | Adulti: {config['adults']} | Max scali: {config['max_stops']}")
    log(f"Periodo desiderato: {config['date_from']} → {config['date_to']} | Notti: {config['nights_min']}-{config['nights_max']}")

    result = generate_date_pairs(config)
    date_pairs, req_start, max_available = result[0], result[1], result[2]

    if not date_pairs:
        log(f"Le date richieste (da {req_start}) non sono ancora disponibili su Google Flights")
        log(f"Google Flights mostra risultati fino a ~{MAX_DAYS_AHEAD} giorni (max: {max_available})")
        log(f"Riproverò automaticamente al prossimo check")
        return

    origins = config["origins"]
    outbound_searches = len(date_pairs) * len(origins)

    # Raccogli date di ritorno uniche per cercare i voli di ritorno
    unique_returns = {}  # (ret_date, origin) → return_info
    for dep_date, ret_date, nights in date_pairs:
        for origin in origins:
            unique_returns[(ret_date, origin)] = None

    return_searches = len(unique_returns)
    total_searches = outbound_searches + return_searches
    est_minutes = (total_searches * (delay + 15)) // 60

    log(f"Ricerche: {outbound_searches} andate + {return_searches} ritorni = {total_searches} totali")
    log(f"Tempo stimato: ~{est_minutes} minuti")

    # FASE 1: Cerca voli di andata (round-trip per avere i prezzi A/R)
    log(f"\n--- FASE 1: Ricerca voli andata (prezzi A/R) ---")
    all_flights = []
    search_count = 0
    errors = 0

    for origin in origins:
        for dep_date, ret_date, nights in date_pairs:
            search_count += 1
            dep_fmt = datetime.strptime(dep_date, "%Y-%m-%d").strftime("%d/%m")
            log(f"  [{search_count}/{outbound_searches}] {get_airport_name(origin)} {dep_fmt} ({nights}n)...")

            result = run_search(
                flight_data=[
                    FlightData(date=dep_date, from_airport=origin, to_airport=config["destination"]),
                    FlightData(date=ret_date, from_airport=config["destination"], to_airport=origin),
                ],
                trip="round-trip",
                adults=config["adults"],
            )
            if result and result.flights:
                flights = process_results(result, origin, dep_date, ret_date, nights, config)
                all_flights.extend(flights)
                log(f"    → {len(flights)} voli validi (di {len(result.flights)} totali)")
            elif result and not result.flights:
                log(f"    → 0 voli trovati")
            else:
                errors += 1

            if search_count < total_searches:
                time.sleep(delay)

    # FASE 2: Cerca dettagli voli di ritorno
    log(f"\n--- FASE 2: Ricerca dettagli ritorni ---")
    ret_count = 0
    for (ret_date, origin) in unique_returns:
        ret_count += 1
        ret_fmt = datetime.strptime(ret_date, "%Y-%m-%d").strftime("%d/%m")
        log(f"  [{ret_count}/{return_searches}] Ritorno {ret_fmt} {config['destination']}→{origin}...")

        ret_info = search_return_flights(config["destination"], origin, ret_date, config["adults"], config)
        if ret_info:
            unique_returns[(ret_date, origin)] = ret_info
            log(f"    → {ret_info['ret_airline']} | {ret_info['ret_duration']} | {ret_info['ret_stops_detail']}")
        else:
            log(f"    → Nessun ritorno trovato")

        if ret_count < return_searches:
            time.sleep(delay)

    # Arricchisci i voli con i dettagli del ritorno
    for f in all_flights:
        ret_date_raw = datetime.strptime(f["ret_date"], "%d/%m/%Y").strftime("%Y-%m-%d")
        ret_info = unique_returns.get((ret_date_raw, f["origin_code"]))
        if ret_info:
            f.update(ret_info)
        else:
            f["ret_airline"] = f["airline"]  # fallback: stessa compagnia
            f["ret_departure"] = ""
            f["ret_arrival"] = ""
            f["ret_duration"] = "N/D"
            f["ret_stops"] = f["stops"]
            f["ret_stops_detail"] = f["stops_detail"]

    # Rimuovi duplicati
    seen = set()
    unique_flights = []
    for f in all_flights:
        key = (f["price_total"], f["dep_date"], f["ret_date"], f["dep_airport"], f["airline"])
        if key not in seen:
            seen.add(key)
            unique_flights.append(f)

    unique_flights.sort(key=lambda x: x["price_pp"])

    log(f"\nRicerche completate: {search_count} (errori: {errors})")
    log(f"Voli unici trovati: {len(unique_flights)}")

    if not unique_flights:
        log("Nessun volo trovato con i criteri specificati")
        return

    save_history(unique_flights)

    log(f"\n{'#'*55}")
    log(f"  TOP 10 VOLI (ordinati per prezzo)")
    log(f"{'#'*55}")
    for i, f in enumerate(unique_flights[:10], 1):
        print(format_flight_text(f, i, config))
    print()

    # Controlla soglia
    good_deals = [f for f in unique_flights if f["price_pp"] <= threshold]

    if good_deals:
        log(f"  {len(good_deals)} VOLI SOTTO €{threshold}/persona!")

        best = good_deals[0]
        send_macos_notification(
            f"Volo {route}!",
            f"€{best['price_pp']:.0f}/pp - {best['dep_date']} da {best['dep_airport']} ({best['nights']}n)"
        )
        send_telegram(config, format_telegram_message(good_deals, threshold, config))
        send_email(config, good_deals, threshold)
        LAST_ALERT_PATH.write_text(datetime.now().isoformat())

        deals_path = SCRIPT_DIR / "deals.txt"
        with open(deals_path, "a") as f:
            f.write(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M')} ---\n")
            for deal in good_deals[:10]:
                f.write(f"€{deal['price_pp']:.0f}/pp | {deal['dep_date']}-{deal['ret_date']} "
                        f"({deal['nights']}n) | {deal['dep_airport']} | {deal['airline']}\n"
                        f"  → {deal.get('link', '')}\n")
        log(f"Offerte salvate in {deals_path}")
    else:
        best = unique_flights[0]
        log(f"Prezzo minimo: €{best['price_pp']:.0f}/persona (soglia: €{threshold})")
        log("Nessun volo sotto soglia, riprovo al prossimo check")

        # Heartbeat: mercoledì dopo le 21, se nessuna email offerte negli ultimi 7 giorni
        now = datetime.now()
        if now.weekday() == 2 and now.hour >= 21:
            send_heartbeat = True
            if LAST_ALERT_PATH.exists():
                try:
                    last_alert = datetime.fromisoformat(LAST_ALERT_PATH.read_text().strip())
                    if (now - last_alert).days < 7:
                        send_heartbeat = False
                except (ValueError, OSError):
                    pass
            if send_heartbeat:
                send_heartbeat_email(config, best["price_pp"], len(unique_flights))
                LAST_ALERT_PATH.write_text(now.isoformat())

    log("Monitoraggio completato\n")


if __name__ == "__main__":
    main()
