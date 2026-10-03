"""Project prelocated stations onto one route and minimize fuel purchases."""

import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP

from .importing import haversine_miles
from .models import FuelStation


METERS_PER_MILE = 1609.344
MILES_PER_GALLON = Decimal('10')
MAX_RANGE_MILES = Decimal('500')
TANK_GALLONS = MAX_RANGE_MILES / MILES_PER_GALLON
FUEL_QUANTUM = Decimal('0.001')
MONEY_QUANTUM = Decimal('0.01')
MAX_OFFSET_MILES = 2.0
GRID_DEGREES = 0.5


class FuelPlanError(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    station: FuelStation
    mile: Decimal
    offset_miles: float


def _cell(latitude, longitude):
    return (math.floor(latitude / GRID_DEGREES),
            math.floor(longitude / GRID_DEGREES))


class RouteProjector:
    def __init__(self, coordinates, distance_meters):
        self.points = [(float(lon), float(lat)) for lon, lat, *_ in coordinates]
        if len(self.points) < 2:
            raise FuelPlanError('The returned route has no usable geometry.')
        self.segment_miles = [
            haversine_miles(lat1, lon1, lat2, lon2)
            for (lon1, lat1), (lon2, lat2) in zip(
                self.points, self.points[1:],
            )
        ]
        self.cumulative_miles = [0.0]
        for length in self.segment_miles:
            self.cumulative_miles.append(self.cumulative_miles[-1] + length)
        if self.cumulative_miles[-1] <= 0:
            raise FuelPlanError('The returned route has zero geometry length.')
        self.route_miles = distance_meters / METERS_PER_MILE
        self.distance_scale = self.route_miles / self.cumulative_miles[-1]
        self.min_lon = min(point[0] for point in self.points)
        self.max_lon = max(point[0] for point in self.points)
        self.min_lat = min(point[1] for point in self.points)
        self.max_lat = max(point[1] for point in self.points)
        self.grid = defaultdict(list)
        for index, (first, second) in enumerate(zip(
            self.points, self.points[1:],
        )):
            if self.segment_miles[index] <= 0:
                continue
            min_lon = min(first[0], second[0]) - 0.15
            max_lon = max(first[0], second[0]) + 0.15
            min_lat = min(first[1], second[1]) - 0.15
            max_lat = max(first[1], second[1]) + 0.15
            low_y, low_x = _cell(min_lat, min_lon)
            high_y, high_x = _cell(max_lat, max_lon)
            for cell_y in range(low_y, high_y + 1):
                for cell_x in range(low_x, high_x + 1):
                    self.grid[(cell_y, cell_x)].append(index)

    def project(self, latitude, longitude):
        """Return route mile and straight-line offset for the nearest segment."""
        earth_radius_miles = 3958.7613
        radians = math.pi / 180
        best = None
        for index in self.grid.get(_cell(latitude, longitude), []):
            (lon1, lat1), (lon2, lat2) = self.points[index:index + 2]
            latitude_scale = math.cos((latitude + lat1 + lat2) / 3 * radians)
            x1 = (lon1 - longitude) * radians * earth_radius_miles * latitude_scale
            y1 = (lat1 - latitude) * radians * earth_radius_miles
            x2 = (lon2 - longitude) * radians * earth_radius_miles * latitude_scale
            y2 = (lat2 - latitude) * radians * earth_radius_miles
            dx, dy = x2 - x1, y2 - y1
            length_squared = dx * dx + dy * dy
            if length_squared == 0:
                continue
            fraction = max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length_squared))
            offset = math.hypot(x1 + fraction * dx, y1 + fraction * dy)
            mile = (self.cumulative_miles[index]
                    + fraction * self.segment_miles[index]) * self.distance_scale
            result = (offset, mile)
            if best is None or result < best:
                best = result
        if best is None or best[0] > MAX_OFFSET_MILES:
            return None
        return best[1], best[0]


def candidates_along_route(projector):
    """Query local data once; the grid limits station-to-segment comparisons."""
    queryset = FuelStation.objects.recommendable().filter(
        latitude__gte=projector.min_lat - 0.15,
        latitude__lte=projector.max_lat + 0.15,
        longitude__gte=projector.min_lon - 0.15,
        longitude__lte=projector.max_lon + 0.15,
    )
    candidates = []
    for station in queryset.iterator(chunk_size=500):
        projection = projector.project(station.latitude, station.longitude)
        if projection is None:
            continue
        mile, offset = projection
        if mile >= projector.route_miles - 0.001:
            continue
        candidates.append(Candidate(
            station,
            Decimal(str(round(max(0.0, mile), 3))),
            round(offset, 3),
        ))
    return sorted(candidates, key=lambda item: (
        item.mile, item.station.price_usd_per_gallon,
        item.station.station_key,
    ))


def _fuel_for_miles(miles):
    return (miles / MILES_PER_GALLON).quantize(
        FUEL_QUANTUM, rounding=ROUND_CEILING,
    )


def optimize_fuel_stops(distance_miles, candidates, starting_fuel_gallons=TANK_GALLONS):
    """Exact lowest purchase cost on a fixed line with zero station detour.

    At each station, buy only enough to reach the first cheaper reachable
    station; if none exists, fill up or buy enough to reach the destination.
    Existing fuel is consumed first. Equal prices can be skipped safely.
    """
    distance = Decimal(str(round(float(distance_miles), 3)))
    starting_fuel = Decimal(str(starting_fuel_gallons))
    if not Decimal('0') <= starting_fuel <= TANK_GALLONS:
        raise ValueError('Starting fuel must be between 0 and 50 gallons.')
    fuel = starting_fuel
    previous_mile = Decimal('0')
    purchases = []
    total_cost = Decimal('0')
    gallons_purchased = Decimal('0')

    for index, candidate in enumerate(candidates):
        mile = candidate.mile
        if mile < previous_mile or mile >= distance:
            continue
        fuel -= (mile - previous_mile) / MILES_PER_GALLON
        if fuel < Decimal('-0.000001'):
            raise FuelPlanError(
                f'No located fuel station is reachable after mile '
                f'{previous_mile:.1f} with the available fuel.'
            )
        fuel = max(Decimal('0'), fuel)
        previous_mile = mile

        target_mile = None
        for future in candidates[index + 1:]:
            if future.mile - mile > MAX_RANGE_MILES:
                break
            if future.station.price_usd_per_gallon < candidate.station.price_usd_per_gallon:
                target_mile = future.mile
                break
        if target_mile is None:
            target_mile = min(distance, mile + MAX_RANGE_MILES)
        required = _fuel_for_miles(target_mile - mile)
        gallons = max(Decimal('0'), required - fuel).quantize(
            FUEL_QUANTUM, rounding=ROUND_CEILING,
        )
        if gallons <= 0:
            continue
        gallons = min(gallons, TANK_GALLONS - fuel)
        if gallons <= 0:
            continue
        fuel += gallons
        price = candidate.station.price_usd_per_gallon
        cost = (gallons * price).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP,
        )
        total_cost += cost
        gallons_purchased += gallons
        station = candidate.station
        purchases.append({
            'station_key': station.station_key,
            'opis_id': station.opis_id,
            'name': station.name,
            'address': station.address,
            'city': station.city,
            'state': station.state,
            'latitude': station.latitude,
            'longitude': station.longitude,
            'location_type': station.location_type,
            'location_source': station.location_source,
            'location_note': station.location_note,
            'source_row_numbers': station.source_row_numbers,
            'price_conflict': station.price_conflict,
            'mile_marker': float(mile),
            'distance_from_route_miles': candidate.offset_miles,
            'price_usd_per_gallon': str(price),
            'gallons_purchased': float(gallons),
            'cost_usd': f'{cost:.2f}',
        })

    fuel -= (distance - previous_mile) / MILES_PER_GALLON
    if fuel < Decimal('-0.000001'):
        raise FuelPlanError(
            f'No located fuel station is reachable after mile '
            f'{previous_mile:.1f} with the available fuel.'
        )
    return {
        'stops': purchases,
        'starting_fuel_gallons': float(starting_fuel),
        'ending_fuel_gallons': round(float(max(Decimal('0'), fuel)), 3),
        'fuel_used_gallons': round(float(distance / MILES_PER_GALLON), 3),
        'fuel_purchased_gallons': float(gallons_purchased),
        'total_cost_usd': f'{total_cost:.2f}',
    }
