# Manual demo: fuel-route API (under five minutes)

## Prepare before recording

1. Start Django from the repository root:

   ```powershell
   .\.venv\Scripts\python.exe manage.py runserver
   ```

2. Import `postman/route-planner.postman_collection.json` into Postman. Its
   `baseUrl` is `http://127.0.0.1:8000`. Open
   `http://127.0.0.1:8000/api/v1/demo/` in a browser tab. The browser form is
   already set to Dallas and Chicago. The preview uses OpenFreeMap vector
   tiles. If an older page still shows blocked OpenStreetMap tiles, restart
   Django and reload the browser tab.
3. Have `fuel_routes/management/commands/import_fuel_prices.py`,
   `fuel_routes/routing.py`, `fuel_routes/planning.py`, `fuel_routes/views.py`,
   and `import-coverage.json` ready to show. Keep `.env` and both API keys off
   screen. Avoid editing code while recording, since Django's development
   server restarts on changes and clears its in-memory route cache.

## Manual test sequence

| Step | Action | What to point out |
| --- | --- | --- |
| 1 | Send **Health** in Postman. | HTTP 200 and `{"status":"ok"}`. |
| 2 | Send **Long route: Dallas to Chicago (fuel stops)**. | HTTP 200; route over 500 miles; two fuel stops; positive `fuel.total_cost_usd`; `map.type` is `FeatureCollection`. Open one stop to show its price, gallons purchased, `location_type`, and `source_row_numbers`. Coordinate input needs at most one directions call. |
| 3 | Send **Repeat long route: cached directions** without restarting Django. | Same route and cost, with `provider_requests.directions: 0` on the repeat request. |
| 4 | In the browser map tab, click **Plan route**. | The line is the driving route; the four point markers are start, finish, and two fuel stops. The browser uses the GeoJSON already returned by the API. |
| 5 | Send **Reject a Canadian start** in Postman. | HTTP 400 with `error.code: "invalid_input"`; the request is rejected before routing. |

The verified Dallas-to-Chicago example on 2026-10-03 returned **966.74 miles**,
fuel stops at **163.665** and **516.949** miles, and **$136.30** spent on fuel
bought during the trip. These figures can change if routing data or the fuel
price file changes. The stable checks are the HTTP status, a route over 500
miles, reachable fuel stops, GeoJSON, and provider call counts. The two displayed
stops use approximate interstate exit coordinates; they are labeled `exit`.

If you have extra time, send **Short route: Austin to Dallas**. It is about 195
miles and returns no fuel stop with `$0.00` spent because the starting tank is
full. **Place names: Dallas to Chicago** demonstrates string inputs; an uncached
request uses two geocoding calls and one directions call.

## Ready-to-say narration

**0:00-0:25, introduction and health:**

> This is a Django 6.1 API for planning a US driving route and where to buy
> fuel. The vehicle has a 500-mile range and gets 10 miles per gallon, so a full
> tank holds 50 gallons. I will start with the health endpoint, then plan a
> route long enough to need fuel.

**0:25-1:45, long route and repeat:**

> Here I send Dallas to Chicago as coordinates. The response includes the
> driving route, two fuel stops, the gallons and price at each stop, and the
> total paid during the trip. With the current data, that is about 967 miles
> and $136.30 in fuel purchases. Fuel already in the starting tank is not
> charged again. The `map` field is GeoJSON, so a client can draw the route and
> markers. This request uses one OpenRouteService directions call. Sending it
> again shows zero directions calls because the route is cached.

**1:45-2:45, browser map and validation:**

> The browser preview draws that GeoJSON route and its stop markers. The stop
> coordinates here are marked as approximate exits, so I do not present them
> as exact pump locations. If I try a starting point in Canada, the API returns
> a 400 error before calling the routing provider.

**2:45-4:40, code and coverage:**

> Station preparation happens once, before requests. The import filters out
> Canadian rows, validates prices, keeps source row IDs, and locates US
> stations using OpenInterstate exits and a conservative geocoder pass.
> Ambiguous locations are excluded. The coverage report shows 2,625
> recommendable stations out of 6,626 unique US stations. At request time,
> `routing.py` gets one route, `planning.py` finds nearby eligible stations and
> minimizes modeled fuel purchases under the 500-mile range, and `views.py`
> validates input and returns the JSON and GeoJSON response. The model uses
> stations within two straight-line miles of the route and does not calculate
> driving detours to individual pumps. The automated tests cover import rules,
> routing, caching, and fuel planning.

**4:40-5:00, close:**

> The API makes at most one directions request for coordinate inputs and uses
> the local station database for fuel planning. The Postman collection and
> setup instructions are in the repository.
