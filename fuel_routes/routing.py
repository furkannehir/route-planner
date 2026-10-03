"""Small OpenRouteService client. A route needs one directions request."""

import hashlib
import json
import math
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache

from .importing import haversine_miles
from .us_boundaries import is_us_location


BASE_URL = 'https://api.heigit.org'
CACHE_SECONDS = 24 * 60 * 60


class RoutingError(Exception):
    def __init__(self, message, status=502, code='routing_provider_error'):
        super().__init__(message)
        self.status = status
        self.code = code


def _cache_key(kind, value):
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()
    return f'fuel-routes:{kind}:{digest}'


def _json_request(request):
    try:
        with urlopen(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            body = json.loads(error.read(4096))
            detail = body.get('error', {})
            if isinstance(detail, dict):
                detail = detail.get('message') or detail.get('code')
            detail = str(detail or body.get('message') or '').strip()
        except (ValueError, TypeError, AttributeError):
            detail = ''
        if error.code in {400, 404, 413}:
            raise RoutingError(
                detail or 'No route could be calculated for these locations.',
                422, 'route_unavailable',
            ) from None
        if error.code in {401, 403}:
            raise RoutingError(
                'The routing service rejected its API key.', 503,
                'routing_configuration_error',
            ) from None
        if error.code == 429:
            raise RoutingError(
                'The routing service rate limit was reached.', 503,
                'routing_rate_limited',
            ) from None
        raise RoutingError(
            'The routing service is unavailable.', 502,
        ) from None
    except (URLError, TimeoutError):
        raise RoutingError(
            'The routing service did not respond in time.', 504,
            'routing_timeout',
        ) from None
    except (ValueError, TypeError):
        raise RoutingError('The routing service returned invalid JSON.') from None


class OpenRouteService:
    def __init__(self, api_key=None):
        self.api_key = api_key if api_key is not None else settings.ORS_API_KEY
        self.directions_requests = 0
        self.geocoding_requests = 0

    def _key(self):
        if not self.api_key:
            raise RoutingError(
                'Set ORS_API_KEY to enable route planning.', 503,
                'routing_not_configured',
            )
        return self.api_key

    def geocode(self, text):
        key = _cache_key('geocode', text.strip().casefold())
        cached = cache.get(key)
        if cached is not None:
            return cached
        params = urlencode({
            'text': text, 'boundary.country': 'USA', 'size': 5,
        })
        request = Request(
            f'{BASE_URL}/pelias/v1/search?{params}',
            headers={'Authorization': self._key(), 'Accept': 'application/json'},
        )
        result = _json_request(request)
        self.geocoding_requests += 1
        if not isinstance(result, dict) or not isinstance(result.get('features'), list):
            raise RoutingError('The geocoding service returned invalid results.')
        plausible = []
        for feature in result.get('features', []):
            if not isinstance(feature, dict):
                continue
            properties = feature.get('properties') or {}
            if not isinstance(properties, dict):
                continue
            if properties.get('country_a', '').upper() not in {'USA', 'US'}:
                continue
            try:
                longitude, latitude = map(
                    float, feature['geometry']['coordinates'][:2],
                )
                confidence = float(properties.get('confidence') or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if (not math.isfinite(confidence) or confidence < 0.7
                    or not is_us_location(latitude, longitude)):
                continue
            plausible.append((confidence, latitude, longitude, properties))
        if not plausible:
            raise RoutingError(
                f'Could not locate {text!r} within the US.', 422,
                'location_not_found',
            )
        plausible.sort(key=lambda item: -item[0])
        best = plausible[0]

        def same_named_locality(other):
            first, second = best[3], other[3]
            return (
                first.get('layer') == second.get('layer') == 'locality'
                and bool(first.get('label'))
                and first['label'].strip().casefold()
                == (second.get('label') or '').strip().casefold()
                and first.get('region_a') == second.get('region_a')
            )
        if any(
            other[0] >= best[0] - 0.05
            and haversine_miles(best[1], best[2], other[1], other[2]) > 10
            and not same_named_locality(other)
            for other in plausible[1:]
        ):
            raise RoutingError(
                f'{text!r} matches multiple US locations; add a state or ZIP.',
                422, 'ambiguous_location',
            )
        location = {
            'latitude': best[1], 'longitude': best[2],
            'label': best[3].get('label') or text,
        }
        cache.set(key, location, CACHE_SECONDS)
        return location

    def directions(self, start, finish):
        pair = [
            [start['longitude'], start['latitude']],
            [finish['longitude'], finish['latitude']],
        ]
        key = _cache_key('route', json.dumps(pair, separators=(',', ':')))
        cached = cache.get(key)
        if cached is not None:
            return cached
        body = json.dumps({
            'coordinates': pair,
            'instructions': False,
            'options': {'avoid_borders': 'all'},
        }).encode('utf-8')
        request = Request(
            f'{BASE_URL}/openrouteservice/v2/directions/driving-car/geojson',
            data=body, method='POST',
            headers={
                'Authorization': self._key(),
                'Content-Type': 'application/json',
                'Accept': 'application/geo+json',
            },
        )
        result = _json_request(request)
        self.directions_requests += 1
        try:
            feature = result['features'][0]
            coordinates = feature['geometry']['coordinates']
            summary = feature['properties']['summary']
            if feature['geometry']['type'] != 'LineString' or len(coordinates) < 2:
                raise ValueError('Missing route geometry.')
            distance = float(summary['distance'])
            duration = float(summary['duration'])
            if not math.isfinite(distance) or not math.isfinite(duration) or distance <= 0:
                raise ValueError('Missing route distance.')
            if any(
                len(point) < 2 or not all(math.isfinite(float(item)) for item in point[:2])
                for point in coordinates
            ):
                raise ValueError('Invalid route coordinates.')
        except (KeyError, IndexError, TypeError, ValueError, OverflowError):
            raise RoutingError('The routing service returned an invalid route.') from None
        route = {
            'geometry': feature['geometry'],
            'distance_meters': distance,
            'duration_seconds': duration,
        }
        cache.set(key, route, CACHE_SECONDS)
        return route
