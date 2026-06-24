"""
Flight Price Scraper — WRO → Włochy/Hiszpania (pary tam+powrót)
===============================================================
Używa Ryanair Booking API (RoundTrip=true) żeby zbierać pary lotów
dla różnych długości pobytu (2,3,4,5,6,7 dni).

Jeden wiersz w bazie = jedna para (tam + powrót) dla konkretnej daty
wylotu i długości pobytu, zescrapowana w konkretnym momencie.

Tryb lokalny:     python scraper.py --days 30
Tryb produkcja:   python scraper.py --days 30 --prod
"""

import argparse
import logging
import random
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Konfiguracja
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ORIGIN = "WRO"

DESTINATIONS = [
    "BGY",  # Mediolan Bergamo
    "CIA",  # Rzym Ciampino
    "BLQ",  # Bolonia
    "NAP",  # Neapol
    "BCN",  # Barcelona
    "AGP",  # Malaga
    "ALC",  # Alicante
    "PMI",  # Palma de Mallorca
    "DUB",  # Dublin
    "ORK",  # Cork
    "SNN",  # Shannon
]

# Długości pobytu do sprawdzenia
STAY_LENGTHS = [2, 3, 4, 5, 6, 7]

DAYS_AHEAD = 30

# Opóźnienia — szanujemy Ryanair, nie spamujemy
DELAY_BETWEEN_REQUESTS = 0.8   # między zapytaniami w jednej trasie
DELAY_BETWEEN_ROUTES   = 2.0   # między trasami

DB_PATH = Path("data/flights.db")

# ---------------------------------------------------------------------------
# Ryanair Booking API — Round Trip
# ---------------------------------------------------------------------------

# Aktualna wersja frontendu (DevTools → Network → Request Headers → client-version)
# Zaktualizuj jeśli po kilku tygodniach API znowu zwraca 409
CLIENT_VERSION = "3.202.0"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
)

BOOKING_API_URL = (
    "https://www.ryanair.com/api/booking/v4/pl-pl/availability"
    "?ADT=1&TEEN=0&CHD=0&INF=0&Disc=0"
    "&Origin={origin}&Destination={dest}"
    "&DateOut={date_out}&DateIn={date_in}"
    "&RoundTrip=true"
    "&FlexDaysBeforeOut=0&FlexDaysOut=0"
    "&FlexDaysBeforeIn=0&FlexDaysIn=0"
    "&IncludeConnectingFlights=false"
    "&ToUs=AGREED&IncludePrimeFares=false"
)


def get_session() -> requests.Session:
    """
    Sesja replikująca nagłówki przeglądarki.
    Wymagania Ryanair API v4 (ustalone z DevTools):
      - client-version: aktualna wersja frontendu
      - client: desktop
      - Cookie fr-correlation-id, rid, xid, mkt
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent":         USER_AGENT,
        "Accept":             "application/json, text/plain, */*",
        "Accept-Language":    "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding":    "gzip, deflate, br, zstd",
        "client":             "desktop",
        "client-version":     CLIENT_VERSION,
        "Referer":            "https://www.ryanair.com/pl/pl/trip/flights/select",
        "Sec-Fetch-Dest":     "empty",
        "Sec-Fetch-Mode":     "cors",
        "Sec-Fetch-Site":     "same-origin",
        "sec-ch-ua":          '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile":   "?0",
        "sec-ch-ua-platform": '"Windows"',
    })
    for name, value in {
        "fr-correlation-id": str(uuid.uuid4()),
        "rid":               str(uuid.uuid4()),
        "xid":               str(uuid.uuid4()),
        "mkt":               "/pl/pl/",
        "RY_COOKIE_CONSENT": "true",
    }.items():
        session.cookies.set(name, value, domain=".ryanair.com", path="/")

    log.debug(f"Session ready | client-version={CLIENT_VERSION}")
    return session


# ---------------------------------------------------------------------------
# Parsowanie odpowiedzi
# ---------------------------------------------------------------------------

def _extract_cheapest_flight(trip_leg: dict) -> dict | None:
    """
    Z jednej nogi podróży (outbound lub inbound) wyciąga najtańszy lot.
    API przy RoundTrip=true może zwrócić kilka lotów na dany dzień —
    bierzemy najtańszy żeby mieć punkt odniesienia dla pary.
    """
    best_price = None
    best = {}

    for date_entry in trip_leg.get("dates", []):
        for flight in date_entry.get("flights", []):
            # Wyciągnij cenę ADT z regularFare
            fare_block = flight.get("regularFare") or flight.get("businessFare")
            if not fare_block:
                continue
            for fare in fare_block.get("fares", []):
                if fare.get("type") == "ADT":
                    price = fare.get("amount")
                    if price is None:
                        continue
                    if best_price is None or price < best_price:
                        best_price = price
                        times = flight.get("time", [])
                        best = {
                            "flight":     flight.get("flightNumber"),
                            "dep_time":   times[0][:16] if times else None,
                            "arr_time":   times[1][:16] if len(times) > 1 else None,
                            "price":      price,
                            "fare_type":  "regular" if flight.get("regularFare") else "business",
                        }
    return best if best_price is not None else None


def parse_round_trip(
    response_json: dict,
    destination: str,
    date_out: str,
    date_in: str,
    stay_length: int,
    scraped_at: str,
) -> dict | None:
    """
    Parsuje odpowiedź Booking API (RoundTrip=true) → jeden wiersz do bazy.
    Struktura odpowiedzi: {"trips": [outbound_trip, inbound_trip], "currency": "PLN"}
    """
    trips = response_json.get("trips", [])
    if len(trips) < 2:
        return None

    # trips[0] = tam (WRO→dest), trips[1] = powrót (dest→WRO)
    outbound_leg = next((t for t in trips if t.get("origin") == ORIGIN), None)
    inbound_leg  = next((t for t in trips if t.get("destination") == ORIGIN), None)

    if not outbound_leg or not inbound_leg:
        return None

    out = _extract_cheapest_flight(outbound_leg)
    inb = _extract_cheapest_flight(inbound_leg)

    if not out or not inb:
        return None

    return {
        "scraped_at":       scraped_at,
        "origin":           ORIGIN,
        "destination":      destination,
        "destination_full": outbound_leg.get("destinationName"),
        "outbound_date":    date_out,
        "outbound_time":    (out["dep_time"] or "")[-5:],   # "HH:MM"
        "outbound_flight":  out["flight"],
        "outbound_price":   out["price"],
        "inbound_date":     date_in,
        "inbound_time":     (inb["dep_time"] or "")[-5:],
        "inbound_flight":   inb["flight"],
        "inbound_price":    inb["price"],
        "total_price":      round(out["price"] + inb["price"], 2),
        "currency":         response_json.get("currency", "PLN"),
        "stay_length":      stay_length,
    }


# ---------------------------------------------------------------------------
# Pobieranie danych
# ---------------------------------------------------------------------------

def fetch_round_trip(
    session: requests.Session,
    destination: str,
    date_out: str,
    date_in: str,
    stay_length: int,
    max_retries: int = 3,
) -> dict | None:
    """Pobiera parę lotów tam+powrót dla konkretnych dat. Retry z backoff."""
    url = BOOKING_API_URL.format(
        origin=ORIGIN,
        dest=destination,
        date_out=date_out,
        date_in=date_in,
    )
    scraped_at = datetime.now(timezone.utc).isoformat()

    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=15)

            if resp.status_code == 200:
                raw = resp.content
                if not raw or not raw.strip():
                    return None
                try:
                    data = resp.json()
                    return parse_round_trip(data, destination, date_out, date_in, stay_length, scraped_at)
                except Exception as e:
                    log.warning(f"  JSON error ({destination} {date_out}→{date_in}): {e}")
                    return None

            elif resp.status_code == 404:
                return None  # brak trasy — normalne

            elif resp.status_code == 409:
                log.warning(f"  409 — refreshing session cookies")
                for name, value in {
                    "fr-correlation-id": str(uuid.uuid4()),
                    "rid": str(uuid.uuid4()),
                }.items():
                    session.cookies.set(name, value, domain=".ryanair.com", path="/")
                time.sleep(3)

            elif resp.status_code == 429:
                wait = 30 * attempt
                log.warning(f"  429 Rate limit — czekam {wait}s")
                time.sleep(wait)

            else:
                log.warning(f"  HTTP {resp.status_code} ({destination} {date_out}→{date_in})")

        except requests.exceptions.Timeout:
            log.warning(f"  Timeout (próba {attempt}/{max_retries})")
        except requests.exceptions.RequestException as e:
            log.warning(f"  Request error: {e}")

        if attempt < max_retries:
            time.sleep(2 ** attempt)

    return None


def scrape_route(session, destination, date_from, date_to) -> list[dict]:
    """
    Dla każdej daty wylotu i każdej długości pobytu pobiera parę lotów.
    Łącznie: days × len(STAY_LENGTHS) zapytań per trasa.
    """
    rows = []
    current = date_from
    total_days = (date_to - date_from).days + 1

    while current <= date_to:
        date_out_str = current.strftime("%Y-%m-%d")

        for stay in STAY_LENGTHS:
            date_in = current + timedelta(days=stay)
            date_in_str = date_in.strftime("%Y-%m-%d")

            row = fetch_round_trip(session, destination, date_out_str, date_in_str, stay)
            if row:
                rows.append(row)
                log.debug(
                    f"  {date_out_str} +{stay}d → {date_in_str} | "
                    f"{row['total_price']} {row['currency']}"
                )

            time.sleep(DELAY_BETWEEN_REQUESTS)

        days_done = (current - date_from).days + 1
        if days_done % 5 == 0:
            log.info(f"  Postęp {ORIGIN}→{destination}: {days_done}/{total_days} dni")

        current += timedelta(days=1)

    log.info(f"  {ORIGIN}→{destination}: {len(rows)} par lotów")
    return rows


# ---------------------------------------------------------------------------
# SQLite (lokalnie)
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS round_trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scraped_at      TEXT    NOT NULL,
    origin          TEXT    NOT NULL,
    destination     TEXT    NOT NULL,
    destination_full TEXT,
    outbound_date   TEXT    NOT NULL,
    outbound_time   TEXT,
    outbound_flight TEXT,
    outbound_price  REAL,
    inbound_date    TEXT    NOT NULL,
    inbound_time    TEXT,
    inbound_flight  TEXT,
    inbound_price   REAL,
    total_price     REAL,
    currency        TEXT,
    stay_length     INTEGER,
    UNIQUE (origin, destination, outbound_date, inbound_date, outbound_time, scraped_at)
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_route       ON round_trips (origin, destination);",
    "CREATE INDEX IF NOT EXISTS idx_outbound    ON round_trips (outbound_date);",
    "CREATE INDEX IF NOT EXISTS idx_stay        ON round_trips (stay_length);",
    "CREATE INDEX IF NOT EXISTS idx_scraped     ON round_trips (scraped_at);",
]


def init_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(CREATE_TABLE_SQL)
    for idx in CREATE_INDEXES_SQL:
        conn.execute(idx)
    conn.commit()
    log.info(f"SQLite gotowe: {db_path.resolve()}")
    return conn


def insert_sqlite(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    inserted = 0
    for row in rows:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO round_trips (
                    scraped_at, origin, destination, destination_full,
                    outbound_date, outbound_time, outbound_flight, outbound_price,
                    inbound_date, inbound_time, inbound_flight, inbound_price,
                    total_price, currency, stay_length
                ) VALUES (
                    :scraped_at, :origin, :destination, :destination_full,
                    :outbound_date, :outbound_time, :outbound_flight, :outbound_price,
                    :inbound_date, :inbound_time, :inbound_flight, :inbound_price,
                    :total_price, :currency, :stay_length
                )
            """, row)
            inserted += conn.execute("SELECT changes()").fetchone()[0]
        except sqlite3.Error as e:
            log.warning(f"  SQLite insert error: {e} | row: {row}")
    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Supabase (produkcja)
# ---------------------------------------------------------------------------

def init_supabase():
    """
    Tabela w Supabase (utwórz raz w SQL Editor):

    CREATE TABLE IF NOT EXISTS round_trips (
        id               BIGSERIAL PRIMARY KEY,
        scraped_at       TIMESTAMPTZ NOT NULL,
        origin           TEXT NOT NULL,
        destination      TEXT NOT NULL,
        destination_full TEXT,
        outbound_date    DATE NOT NULL,
        outbound_time    TEXT,
        outbound_flight  TEXT,
        outbound_price   NUMERIC(10,2),
        inbound_date     DATE NOT NULL,
        inbound_time     TEXT,
        inbound_flight   TEXT,
        inbound_price    NUMERIC(10,2),
        total_price      NUMERIC(10,2),
        currency         TEXT,
        stay_length      INTEGER,
        UNIQUE (origin, destination, outbound_date, inbound_date, outbound_time, scraped_at)
    );
    CREATE INDEX IF NOT EXISTS idx_route    ON round_trips (origin, destination);
    CREATE INDEX IF NOT EXISTS idx_outbound ON round_trips (outbound_date);
    CREATE INDEX IF NOT EXISTS idx_stay     ON round_trips (stay_length);
    CREATE INDEX IF NOT EXISTS idx_scraped  ON round_trips (scraped_at);
    """
    from supabase import create_client
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise EnvironmentError("Brak SUPABASE_URL lub SUPABASE_SERVICE_ROLE_KEY w .env")
    client = create_client(url, key)
    log.info("Supabase połączone.")
    return client


def insert_supabase(client, rows: list[dict]) -> int:
    if not rows:
        return 0
    result = (
        client.table("round_trips")
        .upsert(rows, on_conflict="origin,destination,outbound_date,inbound_date,outbound_time")
        .execute()
    )
    return len(result.data)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Ryanair round-trip scraper WRO→IT/ES")
    parser.add_argument("--prod",  action="store_true", help="Zapisuj do Supabase")
    parser.add_argument("--days",  type=int, default=DAYS_AHEAD)
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.prod:
        log.info("Tryb: PRODUKCJA → Supabase")
        db_client = init_supabase()
        insert_fn = lambda rows: insert_supabase(db_client, rows)
    else:
        log.info("Tryb: LOKALNY → SQLite")
        conn = init_sqlite(DB_PATH)
        insert_fn = lambda rows: insert_sqlite(conn, rows)

    date_from = datetime.now(timezone.utc).date() + timedelta(days=1)
    date_to   = datetime.now(timezone.utc).date() + timedelta(days=args.days)

    log.info(
        f"Zakres: {date_from} → {date_to} ({args.days} dni) | "
        f"Pobyty: {STAY_LENGTHS} dni | "
        f"{len(DESTINATIONS)} tras | "
        f"Łącznie ~{args.days * len(STAY_LENGTHS) * len(DESTINATIONS)} zapytań"
    )

    session = get_session()
    total_inserted = 0

    for dest in DESTINATIONS:
        log.info(f"Scrapuję: {ORIGIN} ↔ {dest}")
        rows = scrape_route(session, dest, date_from, date_to)
        if rows:
            n = insert_fn(rows)
            total_inserted += n
            log.info(f"  Zapisano: {n} par lotów")
        time.sleep(DELAY_BETWEEN_ROUTES + random.uniform(0.5, 1.5))

    log.info(f"Gotowe. Łącznie zapisano: {total_inserted} par lotów.")

    # Podgląd lokalny
    if not args.prod:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT destination, outbound_date, inbound_date, stay_length,
                   outbound_flight, inbound_flight,
                   outbound_price, inbound_price, total_price, currency
            FROM round_trips
            ORDER BY total_price ASC
            LIMIT 10
        """).fetchall()
        print(f"\n{'TRASA':<10} {'TAM':<12} {'POWRÓT':<12} {'DNI':>4} {'CENA':>10} {'WAL'}")
        print("-" * 60)
        for r in rows:
            print(
                f"{ORIGIN}→{r['destination']:<6} "
                f"{r['outbound_date']:<12} {r['inbound_date']:<12} "
                f"{r['stay_length']:>4}d "
                f"{r['total_price']:>9.2f} {r['currency']}"
            )


if __name__ == "__main__":
    main()