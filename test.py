from datetime import datetime, timedelta
from ryanair import Ryanair

api = Ryanair(currency="EUR")
date = datetime.today().date() + timedelta(days=1) # np. jutrzejsza data

# Pobiera WSZYSTKIE loty z WRO do BCN w danym dniu
all_flights = api.get_all_flights("WRO", "BCN", date)

for flight in all_flights:
    print(f"{flight.departureTime} - {flight.flightNumber}: {flight.price} EUR")