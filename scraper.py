"""
Custom Google Flights scraper con Playwright.
Gestisce il cookie consent EU e usa selettori robusti.
"""

import asyncio
import re
from typing import List, Optional
from dataclasses import dataclass

from fast_flights import FlightData, Passengers
from fast_flights.filter import TFSData


@dataclass
class ScrapedFlight:
    airline: str
    departure: str
    arrival: str
    duration: str
    stops: str
    price: str


@dataclass
class SearchResult:
    flights: List[ScrapedFlight]
    raw_html: str = ""


def build_url(flight_data, trip, passengers, seat="economy", max_stops=None, currency="EUR"):
    """Costruisce l'URL di Google Flights usando l'encoding protobuf di fast-flights."""
    tfs = TFSData.from_interface(
        flight_data=flight_data,
        trip=trip,
        passengers=passengers,
        seat=seat,
        max_stops=max_stops,
    )
    data = tfs.as_b64()
    params = {
        "tfs": data.decode("utf-8"),
        "hl": "en",
        "tfu": "EgQIABABIgA",
        "curr": currency,
    }
    return "https://www.google.com/travel/flights?" + "&".join(f"{k}={v}" for k, v in params.items())


async def _fetch_flights(url: str, timeout_ms: int = 45000) -> SearchResult:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            locale="en-US",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 1. Gestisci redirect a consent.google.com
            if "consent.google" in page.url:
                for btn_text in ["Accept all", "Accetta tutto", "Accept", "Accetta"]:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}")')
                        if await btn.count() > 0:
                            await btn.first.click()
                            break
                    except Exception:
                        pass
                try:
                    await page.wait_for_url("**/travel/flights**", timeout=15000)
                except Exception:
                    pass

            # 2. Gestisci overlay di consent sulla pagina stessa
            await page.wait_for_timeout(2000)
            for btn_text in ["Accept all", "Accetta tutto", "Reject all", "Accept"]:
                try:
                    btn = page.locator(f'button:has-text("{btn_text}")')
                    if await btn.count() > 0:
                        await btn.first.click()
                        await page.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # 3. Attendi i risultati volo con selettori multipli
            selectors = [
                '[jsname="IWWDBc"]',
                '[jsname="YdtKid"]',
                'ul.Rk10dc',
                '[role="main"] li',
            ]
            found = False
            for sel in selectors:
                try:
                    await page.wait_for_selector(sel, timeout=timeout_ms)
                    found = True
                    break
                except Exception:
                    continue

            if not found:
                # Fallback: attendi che la pagina sia stabile
                await page.wait_for_load_state("networkidle")
                await page.wait_for_timeout(5000)

            # 4. Estrai dati volo via JavaScript nel contesto della pagina
            flights_data = await page.evaluate("""() => {
                const flights = [];
                // Cerca i container dei voli
                const lists = document.querySelectorAll('ul.Rk10dc');
                for (const list of lists) {
                    const items = list.querySelectorAll('li');
                    for (const item of items) {
                        // Cerca elementi con pattern tipici di Google Flights
                        const allText = item.innerText || '';
                        if (!allText || allText.length < 10) continue;

                        // Prezzo - cerca pattern come €1,234 o $1,234
                        const priceMatch = allText.match(/[€$£][\d,.]+/);
                        const price = priceMatch ? priceMatch[0] : '';

                        if (!price) continue;  // Salta se non c'è prezzo

                        // Estrai tutti i div con testo
                        const spans = item.querySelectorAll('span, div');
                        const texts = [];
                        for (const s of spans) {
                            const t = s.innerText?.trim();
                            if (t && t.length < 100) texts.push(t);
                        }

                        // Orari - pattern HH:MM AM/PM o HH:MM
                        const timePattern = /\d{1,2}:\d{2}(\s*[APap][Mm])?/;
                        const times = texts.filter(t => timePattern.test(t) && t.length < 20);

                        // Durata - pattern come "12 hr 30 min" o "12h 30m"
                        const durPattern = /\d+\s*(hr|h|ore).*\d*\s*(min|m)?/i;
                        const duration = texts.find(t => durPattern.test(t)) || '';

                        // Scali
                        const stopsPattern = /(Nonstop|nonstop|1 stop|2 stops|\\d+ stops?)/i;
                        const stops = texts.find(t => stopsPattern.test(t)) || '';

                        // Compagnia aerea - primo testo significativo che non è orario/durata/prezzo/scali
                        const airline = texts.find(t =>
                            t.length > 2 && t.length < 50 &&
                            !timePattern.test(t) &&
                            !durPattern.test(t) &&
                            !stopsPattern.test(t) &&
                            !/[€$£]/.test(t) &&
                            !/kg|CO2|emissions/i.test(t) &&
                            !/round trip|andata/i.test(t)
                        ) || '';

                        flights.push({
                            airline: airline,
                            departure: times[0] || '',
                            arrival: times[1] || '',
                            duration: duration,
                            stops: stops,
                            price: price,
                        });
                    }
                }

                // Fallback: se non troviamo niente con Rk10dc, cerca con role="main"
                if (flights.length === 0) {
                    const main = document.querySelector('[role="main"]');
                    if (main) {
                        const items = main.querySelectorAll('[jsname="IWWDBc"] li, [jsname="YdtKid"] li');
                        for (const item of items) {
                            const allText = item.innerText || '';
                            const priceMatch = allText.match(/[€$£][\d,.]+/);
                            if (!priceMatch) continue;

                            const timePattern = /\d{1,2}:\d{2}(\s*[APap][Mm])?/;
                            const texts = [];
                            item.querySelectorAll('span, div').forEach(s => {
                                const t = s.innerText?.trim();
                                if (t && t.length < 100) texts.push(t);
                            });
                            const times = texts.filter(t => timePattern.test(t) && t.length < 20);
                            const durPattern = /\d+\s*(hr|h|ore)/i;

                            flights.push({
                                airline: texts.find(t => t.length > 2 && t.length < 50 && !timePattern.test(t) && !/[€$£]/.test(t)) || '',
                                departure: times[0] || '',
                                arrival: times[1] || '',
                                duration: texts.find(t => durPattern.test(t)) || '',
                                stops: texts.find(t => /(nonstop|stop)/i.test(t)) || '',
                                price: priceMatch[0],
                            });
                        }
                    }
                }

                return flights;
            }""")

            raw_html = await page.evaluate(
                "() => document.querySelector('[role=\"main\"]')?.innerHTML || ''"
            )

        finally:
            await browser.close()

    flights = [ScrapedFlight(**f) for f in flights_data]
    return SearchResult(flights=flights, raw_html=raw_html)


def search_flights(flight_data, trip, passengers, seat="economy", max_stops=None, currency="EUR", timeout_ms=45000):
    """Cerca voli su Google Flights. Sincrono."""
    url = build_url(flight_data, trip, passengers, seat, max_stops, currency)
    return asyncio.run(_fetch_flights(url, timeout_ms))
