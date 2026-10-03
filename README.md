# Route Planner API

Django 6.1.1 API for a US driving route and cost-focused fuel stops. It uses a
one-time local station import and one OpenRouteService directions request for
coordinate inputs. Its `map` response is GeoJSON with route and stop markers.

## Local setup (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env and set ORS_API_KEY to your private OpenRouteService key.
.\.venv\Scripts\python.exe manage.py migrate
```

After importing stations below, run:

```powershell
.\.venv\Scripts\python.exe manage.py runserver
```

The API runs at `http://127.0.0.1:8000/`; the interactive map
preview is at `http://127.0.0.1:8000/api/v1/demo/`. Django uses SQLite for
development. The ignored `.env` file is loaded at startup, while environment
variables take precedence. Set `DJANGO_DEBUG=0`, `DJANGO_SECRET_KEY`, and
`DJANGO_ALLOWED_HOSTS` for deployment. Keep API keys out of Git.

## Route API

`POST /api/v1/routes/` accepts US coordinates or place names for `start` and
`finish`. Coordinates are objects with `latitude` and `longitude`; place names
are strings such as `"Dallas, TX"`. `starting_fuel_gallons` is optional and
defaults to a full 50-gallon tank.

```powershell
$body = @{
  start = @{ latitude = 30.2672; longitude = -97.7431 }
  finish = @{ latitude = 32.7767; longitude = -96.7970 }
  starting_fuel_gallons = 50
} | ConvertTo-Json -Depth 3
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/v1/routes/ -ContentType application/json -Body $body
```

The response contains `route` distance and duration, `fuel_stops` with prices,
gallons bought and source row numbers, `fuel.total_cost_usd`, and a `map`
GeoJSON FeatureCollection with the route, start, finish, and fuel-stop points.
`provider_requests` records how many external requests occurred on that call.
Coordinates use one directions request; place names add up to two geocoding
requests. Identical searches and routes are cached for 24 hours per server
process. Station lookup and optimization use only local data. Invalid inputs
return 400; unrouteable trips or fuel coverage gaps return 422; missing keys
or unavailable providers return 503/502/504.

Fuel use is route miles divided by 10 mpg. A full tank provides at most 500
miles. `total_cost_usd` is the money spent *during this trip* on fuel bought at
recommended stops; it does not assign a price to fuel already in the tank.
Consequently, a trip under 500 miles can cost `$0.00` while still consuming
fuel. The optimizer minimizes purchase cost on the fixed returned route, using
located stations within two straight-line miles. It does not calculate exact
drivable detours or pump access; approximate exit markers are labeled in the
response. A route with no reachable eligible station returns 422 rather than
inventing a stop. [OpenRouteService limits public driving directions to 6,000
km](https://openrouteservice.org/restrictions/).

The browser map preview uses Leaflet and OpenStreetMap tiles for a human
viewing the returned GeoJSON. The API itself returns the geometry; clients can
render it with any GeoJSON map library. The preview requires internet access
for its tiles and Leaflet.

## One-time fuel station import

The supplied CSV has no coordinates, and most addresses describe road exits.
Download the pinned [OpenInterstate release](https://openinterstate.org/releases/)
and the free [GeoNames US gazetteer](https://www.geonames.org/export/) into an
ignored local cache, then run the import:

```powershell
New-Item -ItemType Directory -Path .cache -Force | Out-Null
curl.exe -L --fail -o .cache/openinterstate-release-2026-07-13-gha-33.tar.gz https://github.com/tldev/openinterstate/releases/download/release-2026-07-13-gha-33/openinterstate-release-2026-07-13-gha-33.tar.gz
curl.exe -L --fail -o .cache/geonames-US.zip https://download.geonames.org/export/dump/US.zip
.\.venv\Scripts\python.exe manage.py import_fuel_prices
```

On macOS/Linux, use the same archive URLs with `curl -L --fail -o`, then run
`python manage.py import_fuel_prices` from an activated virtual environment.
After the import, start the server with `python manage.py runserver`.

The command verifies the pinned OpenInterstate SHA-256, imports all CSV records
with their source record numbers, and writes `import-coverage.json`. It can be
rerun safely; the current stations and source rows are replaced in one database
transaction, while import run summaries are retained. The route API queries
the local database and never geocodes stations per request.

The import uses these rules:

- Only US state and DC rows are considered for recommendations; Canadian rows
  remain in the source-row audit table with `filtered_non_us` status.
- Prices must be finite, greater than zero, and at most $25 per gallon. Rows
  failing validation remain in the audit table.
- Station identity is OPIS ID plus address, city, and state. For conflicting
  prices at one station, use the median of *distinct* listed prices, mark
  `price_conflict`, and preserve every source record number. This is an estimate:
  the CSV has no price timestamps or fuel grade column.
- A named gas POI is recorded as `station`; an unambiguous matched interstate
  exit is recorded as `exit`, with an approximate coordinate. Ambiguous and
  unresolved locations are never recommendation candidates. The city gazetteer
  disambiguates repeated exit numbers; a city center is never substituted for
  a station coordinate. Rows with several interstate/exit clauses are checked
  against each clause. Separate same-brand POIs near one exit are treated as
  ambiguous, even when they are close together. A matching gas POI near the
  listed town without a matching road or exit is also ambiguous and excluded.

The current offline import resolves **2,619 of 6,626** unique US stations
(690 gas POIs linked to a matching exit and 1,929 approximate exits). The
remaining 4,007 locations are recorded as ambiguous or unresolved in
`import-coverage.json`, which also breaks down exclusion reasons. This includes
321 town-level gas POI candidates that lack a matching road or exit. A `station`
coordinate is the matched POI's point, while an `exit` coordinate approximates
station access.

An optional one-time [Geoapify geocoding](https://www.geoapify.com/geocoding-api/)
pass can attempt the remaining records. Set `GEOAPIFY_API_KEY` privately, then
run `manage.py import_fuel_prices --geocode`. Its results are cached in `.cache`,
the command defaults to at most 2,500 geocoding calls per UTC day, and only
strong US station/POI matches are accepted. A POI must match the store number
or a distinctive full name; a chain brand alone is insufficient. Results that
resolve only to a city or give multiple plausible locations remain excluded.
Geocoded POIs must also match the listed state and city and lie within eight
miles of the listed town.
The local import report currently records zero Geoapify matches because no API
key was supplied. With a key, run the import before serving route requests; if
the daily request cap is reached, rerun it on the next UTC day to finish the
cached backlog. No Geoapify calls are made by the normal import or by API
requests.

OpenInterstate is derived from OpenStreetMap and licensed under ODbL. GeoNames
is licensed under CC BY 4.0. Coordinate inputs are validated against the
[US Census 2025 state boundaries](https://www.census.gov/geographies/mapping-files/time-series/geo/cartographic-boundary.2025.html),
stored as compressed KML in `data/`. Display source attribution with any map
that uses the derived locations. The browser preview includes OpenStreetMap
tile attribution and is intended for manual demo use.

## Verification and delivery

Run `manage.py test fuel_routes` and `manage.py check`. Tests cover import
rules, coordinate validation, mocked provider requests, route caching, fuel
cost, and reachability. Import the Postman collection in `postman/` and follow
`DEMO.md` for a five-minute walkthrough. Live routing requires an ORS key; the
station enrichment pass requires a Geoapify key.

## Project layout

- `config/`: Django settings and root URL configuration.
- `fuel_routes/`: API application, models, import code and management command.
- `data/`: official Census state boundary KML for offline coordinate validation.
- `fuel-prices-for-be-assessment.csv`: supplied fuel prices.
- `import-coverage.json`: counts and source hashes from the latest local import.
- `postman/`: API requests for the demo.
- `DEMO.md`: recording outline.
