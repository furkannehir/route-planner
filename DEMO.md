# Five-minute Loom walkthrough

Record after setting `ORS_API_KEY` in the ignored `.env` file, importing fuel
stations, and checking a long route that returns at least one fuel stop. Use
the Postman collection in `postman/route-planner.postman_collection.json`.

| Time | Show |
| --- | --- |
| 0:00–0:30 | Explain the request: US start and finish, 500-mile tank range, 10 mpg, lowest fuel purchase cost on the returned route. |
| 0:30–1:30 | In Postman, send the short coordinate request. Show `route`, GeoJSON `map`, and `provider_requests.directions` (one on the first request, zero if cached). |
| 1:30–2:30 | Send a verified route longer than 500 miles. Show multiple `fuel_stops` if present, gallons bought, prices, and `fuel.total_cost_usd`. |
| 2:30–3:10 | Open `/api/v1/demo/` in a browser and draw that route and its markers. Note that the map tiles do not trigger extra routing requests. |
| 3:10–3:40 | Send the Canadian-start example and show the 400 response. Explain that a route with no reachable eligible fuel station returns 422. |
| 3:40–4:35 | In code, show `import_fuel_prices.py`, `routing.py`, `planning.py`, and `views.py`: one-time import, one directions call, local station selection, response. |
| 4:35–5:00 | Show `import-coverage.json`, run the tests, and mention the source/accuracy limits. |

Avoid showing `.env` or API keys in the recording. A short trip may correctly
show `$0.00` spent because the default starting tank is full; use a longer
verified route to demonstrate purchases.
