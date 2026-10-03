import json
import math
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .importing import haversine_miles
from .models import FuelImportRun
from .planning import (
    MAX_RANGE_MILES, MILES_PER_GALLON, TANK_GALLONS, FuelPlanError,
    RouteProjector, candidates_along_route, optimize_fuel_stops,
)
from .routing import OpenRouteService, RoutingError
from .us_boundaries import is_us_location


@require_GET
def health(request):
    return JsonResponse({'status': 'ok'})


@require_GET
@never_cache
def demo(request):
    return render(request, 'fuel_routes/demo.html')


class InputError(Exception):
    pass


def _json_error(code, detail, status):
    return JsonResponse({'error': {'code': code, 'detail': detail}}, status=status)


def _coordinate(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f'{field} must be a number.')
    try:
        number = float(value)
    except OverflowError:
        raise InputError(f'{field} is outside the coordinate range.') from None
    if not math.isfinite(number):
        raise InputError(f'{field} must be finite.')
    return number


def _location(value, field, service):
    if isinstance(value, str):
        text = value.strip()
        if not 3 <= len(text) <= 150:
            raise InputError(f'{field} must contain 3 to 150 characters.')
        return service.geocode(text)
    if not isinstance(value, dict):
        raise InputError(
            f'{field} must be a US place name or an object with latitude and longitude.'
        )
    latitude = _coordinate(value.get('latitude'), f'{field}.latitude')
    longitude = _coordinate(value.get('longitude'), f'{field}.longitude')
    if not is_us_location(latitude, longitude):
        raise InputError(f'{field} must be within a US state or DC.')
    return {
        'latitude': latitude,
        'longitude': longitude,
        'label': value.get('label') if isinstance(value.get('label'), str) else None,
    }


def _starting_fuel(value):
    if isinstance(value, bool):
        raise InputError('starting_fuel_gallons must be between 0 and 50.')
    try:
        fuel = Decimal(str(value))
        if not fuel.is_finite() or not 0 <= fuel <= TANK_GALLONS:
            raise ValueError
    except (InvalidOperation, TypeError, ValueError):
        raise InputError('starting_fuel_gallons must be between 0 and 50.') from None
    return fuel


def _reject_nonfinite(value):
    raise ValueError(f'JSON number {value} is not finite.')


@csrf_exempt
@require_POST
def plan_route(request):
    if not request.content_type.startswith('application/json'):
        return _json_error('invalid_content_type', 'Send JSON.', 415)
    if len(request.body) > 8192:
        return _json_error('request_too_large', 'Request body is too large.', 413)
    try:
        payload = json.loads(
            request.body,
            parse_constant=_reject_nonfinite,
        )
        if not isinstance(payload, dict):
            raise InputError('The JSON body must be an object.')
        if 'start' not in payload or 'finish' not in payload:
            raise InputError('Both start and finish are required.')
        starting_fuel = _starting_fuel(
            payload.get('starting_fuel_gallons', TANK_GALLONS),
        )
    except (ValueError, UnicodeDecodeError):
        return _json_error('invalid_json', 'Request body is not valid JSON.', 400)
    except InputError as error:
        return _json_error('invalid_input', str(error), 400)

    if not FuelImportRun.objects.exists():
        return _json_error(
            'stations_not_imported',
            'Run manage.py import_fuel_prices before serving route requests.',
            503,
        )

    service = OpenRouteService()
    try:
        start = _location(payload['start'], 'start', service)
        finish = _location(payload['finish'], 'finish', service)
        if haversine_miles(
            start['latitude'], start['longitude'],
            finish['latitude'], finish['longitude'],
        ) < 0.06:
            raise InputError('Start and finish must be different locations.')
        route = service.directions(start, finish)
        distance_miles = route['distance_meters'] / 1609.344
        if Decimal(str(distance_miles)) <= starting_fuel * MILES_PER_GALLON:
            candidates = []
        else:
            projector = RouteProjector(
                route['geometry']['coordinates'], route['distance_meters'],
            )
            candidates = candidates_along_route(projector)
        fuel_plan = optimize_fuel_stops(
            distance_miles, candidates, starting_fuel,
        )
    except InputError as error:
        return _json_error('invalid_input', str(error), 400)
    except RoutingError as error:
        return _json_error(error.code, str(error), error.status)
    except FuelPlanError as error:
        return _json_error('insufficient_fuel_coverage', str(error), 422)

    stops = fuel_plan.pop('stops')
    features = [{
        'type': 'Feature',
        'geometry': route['geometry'],
        'properties': {
            'kind': 'route', 'distance_miles': round(distance_miles, 2),
        },
    }]
    for kind, location in (('start', start), ('finish', finish)):
        features.append({
            'type': 'Feature',
            'geometry': {
                'type': 'Point',
                'coordinates': [location['longitude'], location['latitude']],
            },
            'properties': {'kind': kind, 'label': location.get('label') or kind.title()},
        })
    features.extend({
        'type': 'Feature',
        'geometry': {
            'type': 'Point',
            'coordinates': [stop['longitude'], stop['latitude']],
        },
        'properties': {
            'kind': 'fuel_stop', 'station_key': stop['station_key'],
            'name': stop['name'], 'mile_marker': stop['mile_marker'],
            'price_usd_per_gallon': stop['price_usd_per_gallon'],
            'location_type': stop['location_type'],
        },
    } for stop in stops)
    return JsonResponse({
        'start': start,
        'finish': finish,
        'route': {
            'distance_miles': round(distance_miles, 2),
            'duration_minutes': round(route['duration_seconds'] / 60, 1),
        },
        'fuel_stops': stops,
        'fuel': {
            'tank_capacity_gallons': float(TANK_GALLONS),
            'maximum_range_miles': float(MAX_RANGE_MILES),
            'miles_per_gallon': float(MILES_PER_GALLON),
            **fuel_plan,
        },
        'map': {'type': 'FeatureCollection', 'features': features},
        'provider_requests': {
            'directions': service.directions_requests,
            'geocoding': service.geocoding_requests,
        },
        'assumptions': {
            'cost': 'Only fuel purchased during the trip is charged; fuel already in the tank is not priced.',
            'stop_selection': 'Minimum purchase cost on this fixed route using located stations within two straight-line miles.',
            'access': 'Station detours and pump access are not routed; exit locations are approximate.',
        },
        'attribution': [
            'OpenRouteService / HeiGIT', 'OpenStreetMap contributors',
            'OpenInterstate', 'GeoNames', 'US Census Bureau',
            'Geoapify: https://www.geoapify.com/',
        ],
    })
