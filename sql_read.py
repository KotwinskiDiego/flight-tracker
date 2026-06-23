import sqlite3

# Połączenie
conn = sqlite3.connect("data/flights.db")
conn.row_factory = sqlite3.Row  # umożliwia odwołanie po nazwach kolumn
cursor = conn.cursor()

# Wykonaj zapytanie
cursor.execute("SELECT * FROM round_trips")

# Pobierz wszystkie wiersze
rows = cursor.fetchall()

# Wyświetl nagłówki
if rows:
    headers = rows[0].keys()
    print(" | ".join(headers))
    print("-" * 80)

    # Wyświetl dane
    for row in rows:
        print(" | ".join(str(row[col]) for col in headers))

# Zamknij połączenie
conn.close()