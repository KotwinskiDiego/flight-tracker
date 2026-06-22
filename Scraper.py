"""
Flight Price Scraper — WRO → Włochy/Hiszpania (loty powrotne)
===============================================================
Zbiera pary lotów (tam i z powrotem) dla różnych długości pobytu.
Tryb lokalny: zapisuje do SQLite (data/flights.db)
Tryb produkcyjny: zapisuje do Supabase (Postgres)

Uruchomienie:
    python scraper.py              # lokalnie → SQLite
    python scraper.py --prod       # produkcja → Supabase
"""

import argparse
import logging
import sqlite3
import time
from datetime import datetime, timedelta,timezone
from pathlib import Path
import os

from dotenv import load_dotenv
from ryanair import Ryanair
from ryanair.types import Trip

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

# Destynacje — możesz rozszerzyć
DESTINATIONS = [
    "BGY", "CIA", "BLQ", "NAP", "BCN", "AGP", "ALC", "PMI",
]

# Długości pobytu (w dniach) do sprawdzenia
STAY_LENGTHS = [3, 4, 5, 7, 10, 14]  # możesz dodać więcej

# Liczba dni w przód do sprawdzenia (data wylotu)
DAYS_AHEAD = 30  # zmniejszyłem z 90 do 30, żeby nie przeciążać API

# Opóźnienie między zapytaniami
REQUEST_DELAY_SEC = 1.0

DB_PATH = Path("data/flights.db")


# ---------------------------------------------------------------------------
# Baza danych — nowa struktura (jeden wiersz = jedna para lotów)
# ---------------------------------------------------------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS round_trips (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    scraped_at          TEXT NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    destination_full    TEXT,
    outbound_date       TEXT NOT NULL,
    outbound_time       TEXT,
    outbound_flight     TEXT,
    outbound_price      REAL,
    inbound_date        TEXT NOT NULL,
    inbound_time        TEXT,
    inbound_flight      TEXT,
    inbound_price       REAL,
    total_price         REAL,
    currency            TEXT,
    stay_length         INTEGER,
    UNIQUE(origin, destination, outbound_date, inbound_date, outbound_time)
);
"""


def init_sqlite(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(CREATE_TABLE_SQL)
    conn.commit()
    log.info(f"SQLite gotowe: {db_path.resolve()}")
    return conn


def insert_sqlite(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    conn.executemany("""
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
    """, rows)
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Supabase (produkcja)
# ---------------------------------------------------------------------------

def init_supabase():
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
    result = client.table("round_trips").upsert(rows, on_conflict="origin, destination, outbound_date, inbound_date, outbound_time").execute()
    return len(result.data)


# ---------------------------------------------------------------------------
# Scraping — pary lotów dla różnych długości pobytu
# ---------------------------------------------------------------------------

def scrape_return_route(api: Ryanair, destination: str, date_from, date_to, stay_lengths: list[int]) -> list[dict]:
    """
    Pobiera pary lotów (tam i z powrotem) dla każdej daty wylotu i długości pobytu.
    Używa poprawnej składni: 5 argumentów pozycyjnych (BEZ nazw).
    """
    scraped_at = datetime.now(timezone.utc).isoformat()
    rows = []
    
    current_date = date_from
    delta = timedelta(days=1)
    
    while current_date <= date_to:
        for stay in stay_lengths:
            return_date = current_date + timedelta(days=stay)
            
            try:
                # ✅ POPRAWNA SKŁADNIA – 5 argumentów pozycyjnych, BEZ nazw!
                trips: list[Trip] = api.get_cheapest_return_flights(
                    ORIGIN,          # 1. origin
                    current_date,    # 2. date_from
                    current_date,    # 3. date_to (ten sam dzień)
                    return_date,     # 4. return_date_from
                    return_date      # 5. return_date_to
                )
                
                for trip in trips:
                    # Filtruj po destynacji (na всякий случай)
                    if trip.outbound.destination != destination:
                        continue
                    
                    row = {
                        "scraped_at": scraped_at,
                        "origin": ORIGIN,
                        "destination": destination,
                        "destination_full": getattr(trip.outbound, "destinationFull", None),
                        "outbound_date": trip.outbound.departureTime.date().isoformat(),
                        "outbound_time": trip.outbound.departureTime.strftime("%H:%M"),
                        "outbound_flight": getattr(trip.outbound, "flightNumber", None),
                        "outbound_price": trip.outbound.price,
                        "inbound_date": trip.inbound.departureTime.date().isoformat(),
                        "inbound_time": trip.inbound.departureTime.strftime("%H:%M"),
                        "inbound_flight": getattr(trip.inbound, "flightNumber", None),
                        "inbound_price": trip.inbound.price,
                        "total_price": trip.totalPrice,
                        "currency": getattr(trip.outbound, "currency", "EUR"),
                        "stay_length": stay,
                    }
                    rows.append(row)
                    
            except Exception as exc:
                log.warning(f"Błąd dla {ORIGIN}→{destination} (wylot {current_date}, pobyt {stay}d): {exc}")
            
            time.sleep(0.2)  # opóźnienie między zapytaniami
        
        current_date += delta
        
        # Log postępu co 10 dni
        days_done = (current_date - date_from).days
        total_days = (date_to - date_from).days
        
        if days_done % 10 == 0:
            log.info(f"Postęp: {days_done}/{total_days} dni dla {destination}")
    
    log.info(f"  {ORIGIN}↔{destination}: pobrano {len(rows)} par lotów")
    return rows

# ---------------------------------------------------------------------------
# Wejście główne
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Flight price scraper WRO → IT/ES (loty powrotne)")
    parser.add_argument("--prod", action="store_true", help="Zapisuj do Supabase zamiast SQLite")
    parser.add_argument("--days", type=int, default=DAYS_AHEAD, help=f"Ile dni w przód (domyślnie {DAYS_AHEAD})")
    return parser.parse_args()


def main():
    args = parse_args()

    # Inicjalizacja backendów
    if args.prod:
        log.info("Tryb: PRODUKCJA → Supabase")
        db_client = init_supabase()
        insert_fn = lambda rows: insert_supabase(db_client, rows)
    else:
        log.info("Tryb: LOKALNY → SQLite")
        conn = init_sqlite(DB_PATH)
        insert_fn = lambda rows: insert_sqlite(conn, rows)

    date_from = datetime.utcnow().date() + timedelta(days=1)
    date_to = datetime.utcnow().date() + timedelta(days=args.days)
    
    log.info(f"Zakres dat wylotu: {date_from} → {date_to} ({args.days} dni)")
    log.info(f"Długości pobytu: {STAY_LENGTHS} dni")
    
    api = Ryanair(currency="EUR")

    total_inserted = 0
    for dest in DESTINATIONS:
        log.info(f"Scrapuję pary: {ORIGIN} ↔ {dest}")
        rows = scrape_return_route(api, dest, date_from, date_to, STAY_LENGTHS)
        if rows:
            n = insert_fn(rows)
            total_inserted += n
            log.info(f"  Zapisano: {n} par lotów")
        time.sleep(REQUEST_DELAY_SEC)

    log.info(f"Gotowe. Łącznie zapisano: {total_inserted} par lotów.")

    # Podgląd
    if not args.prod:
        log.info("\n--- Podgląd ostatnich 5 par (SQLite) ---")
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM round_trips ORDER BY id DESC LIMIT 5"
        ).fetchall()
        for r in rows:
            print(
                f"{r['outbound_date']} {r['outbound_time']} → {r['inbound_date']} {r['inbound_time']} | "
                f"{r['origin']}→{r['destination']} | "
                f"pobyt {r['stay_length']}d | "
                f"cena: {r['total_price']} {r['currency']}"
            )


if __name__ == "__main__":
    main()