"""Optional, one-time Geoapify enrichment for stations without a safe exit match."""

import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .importing import _brand_tokens, haversine_miles, normalized_city, normalized_text


class GeocodingError(Exception):
    pass


def _specific_name_match(source, candidate):
    source_words = _brand_tokens(source)
    candidate_words = _brand_tokens(candidate)
    if not source_words or source_words != candidate_words:
        return False
    source_store_numbers = set(re.findall(r'#\s*(\d+)\b', source))
    if source_store_numbers:
        return source_store_numbers == set(re.findall(r'#\s*(\d+)\b', candidate))
    return len(source_words) >= 3


def read_geocode_cache(path):
    cache = {}
    path = Path(path)
    if path.exists():
        with path.open('r', encoding='utf-8') as source:
            for line in source:
                if line.strip():
                    item = json.loads(line)
                    cache[item['station_key']] = item['response']
    return cache


class GeoapifyGeocoder:
    def __init__(self, api_key, cache_path, max_requests=2500):
        if not api_key:
            raise ValueError('GEOAPIFY_API_KEY is required for geocoding.')
        self.api_key = api_key
        self.cache_path = Path(cache_path)
        self.max_requests = max_requests
        self.requests_made = 0
        self.requests_made_today = 0
        self.request_failures = 0
        self.cache = read_geocode_cache(self.cache_path)
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        today = datetime.now(timezone.utc).date()
        if self.cache_path.exists():
            with self.cache_path.open('r', encoding='utf-8') as source:
                for line in source:
                    if line.strip():
                        item = json.loads(line)
                        requested_at = item.get('requested_at')
                        if requested_at and datetime.fromisoformat(requested_at).date() == today:
                            self.requests_made_today += 1

    def lookup(self, station):
        with self._lock:
            if station.station_key in self.cache:
                return self.cache[station.station_key]
        query = f'{station.name}, {station.city}, {station.state}, USA'
        if station.address[:1].isdigit():
            query = f'{station.address}, {station.city}, {station.state}, USA'
        params = urlencode({
            'text': query, 'filter': 'countrycode:us',
            'limit': 10, 'format': 'geojson',
        })
        request = Request(
            f'https://api.geoapify.com/v1/geocode/search?{params}',
            headers={
                'x-api-key': self.api_key,
                'Accept': 'application/geo+json',
                'User-Agent': 'route-planner-assessment/1.0',
            },
        )
        for attempt in range(3):
            with self._lock:
                if self.requests_made_today >= self.max_requests:
                    return None
                delay = self._next_request_at - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._next_request_at = time.monotonic() + 0.22
                self.requests_made_today += 1
                self.requests_made += 1
            try:
                with urlopen(request, timeout=20) as response:
                    result = json.load(response)
                break
            except HTTPError as error:
                if error.code in {401, 403}:
                    raise GeocodingError(
                        f'Geoapify rejected the API key (HTTP {error.code}).'
                    ) from None
                if error.code not in {429, 500, 502, 503, 504}:
                    raise GeocodingError(
                        f'Geoapify returned HTTP {error.code}.'
                    ) from None
            except (URLError, TimeoutError, ValueError):
                pass
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
        else:
            with self._lock:
                self.request_failures += 1
            return None
        with self._lock:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open('a', encoding='utf-8') as target:
                target.write(json.dumps({
                    'station_key': station.station_key,
                    'requested_at': datetime.now(timezone.utc).isoformat(),
                    'response': result,
                }, separators=(',', ':')) + '\n')
            self.cache[station.station_key] = result
        return result


def resolve_geocoded_station(station, response, city_centers):
    if not response:
        return None
    matches = []
    source_address = normalized_text(station.address)
    for feature in response.get('features', []):
        properties = feature.get('properties') or {}
        if properties.get('country_code', '').lower() != 'us':
            continue
        if properties.get('state_code', '').upper() != station.state:
            continue
        result_city = properties.get('city') or properties.get('town')
        if result_city and normalized_city(result_city) != normalized_city(station.city):
            continue
        if float((properties.get('rank') or {}).get('confidence') or 0) < 0.8:
            continue
        result_type = properties.get('result_type')
        is_named_poi = (
            result_type == 'amenity'
            and _specific_name_match(station.name, properties.get('name') or '')
        )
        is_address = (
            result_type == 'building' and source_address
            and source_address == normalized_text(properties.get('address_line1'))
        )
        if not (is_named_poi or is_address):
            continue
        coordinates = feature.get('geometry', {}).get('coordinates', [])
        if len(coordinates) < 2:
            continue
        longitude, latitude = map(float, coordinates[:2])
        centers = city_centers.get((station.state, normalized_city(station.city)), [])
        if centers and min(
            haversine_miles(latitude, longitude, lat, lon)
            for lat, lon, _ in centers
        ) > 8:
            continue
        matches.append((latitude, longitude, properties.get('place_id', '')))

    if not matches:
        return None
    first = matches[0]
    if any(haversine_miles(first[0], first[1], lat, lon) > 0.1
           for lat, lon, _ in matches[1:]):
        return {
            'location_type': 'ambiguous',
            'location_note': 'Geocoder returned multiple plausible station locations.',
        }
    return {
        'location_type': 'station', 'latitude': first[0],
        'longitude': first[1], 'location_source': 'geoapify',
        'location_reference': first[2],
        'location_note': 'Named POI or full building address matched by geocoder.',
    }
