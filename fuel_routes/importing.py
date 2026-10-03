"""One-time preparation of the supplied prices for route queries."""

import csv
import hashlib
import io
import json
import math
import re
import statistics
import tarfile
import unicodedata
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path


US_STATES = frozenset(
    'AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN '
    'MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA '
    'WA WV WI WY DC'.split()
)
PRICE_QUANTUM = Decimal('0.00000001')
INTERSTATE_RE = re.compile(r'\bI\s*[- ]\s*(\d{1,3}[EW]?)\b', re.I)
EXIT_RE = re.compile(r'\bEXIT\s*(\d{1,4}(?:\s*-?\s*[A-Z])?)\b', re.I)
WORD_RE = re.compile(r'[A-Z0-9]+')
GENERIC_NAME_WORDS = frozenset(
    'A AND AT CENTER CENTERS CONVENIENCE FUEL GAS INC MART OF PLAZA STORE '
    'STORES STOP STOPS STATION THE TRAVEL TRUCK TRUCKSTOP TRUCKSTOPS'.split()
)


@dataclass(frozen=True)
class PriceRecord:
    source_row_number: int
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    price: Decimal | None
    status: str
    station_key: str = ''


@dataclass(frozen=True)
class StationInput:
    station_key: str
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    price: Decimal
    price_conflict: bool
    source_row_numbers: list[int]


@dataclass(frozen=True)
class ExitPoint:
    exit_id: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class GasPlace:
    place_id: str
    name: str
    brand: str
    latitude: float
    longitude: float


def normalized_text(value):
    value = unicodedata.normalize('NFKD', value or '')
    value = ''.join(c for c in value if not unicodedata.combining(c))
    return ' '.join(WORD_RE.findall(value.upper()))


def normalized_city(value):
    # The CSV contains spacing variants such as "Mc Calla" versus "McCalla".
    return normalized_text(value).replace(' ', '')


def station_key(opis_id, address, city, state):
    identity = '|'.join(normalized_text(x) for x in (opis_id, address, city, state))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_prices(path):
    records = []
    grouped = defaultdict(list)
    with Path(path).open('r', encoding='utf-8-sig', newline='') as source:
        reader = csv.DictReader(source)
        expected = {
            'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City',
            'State', 'Rack ID', 'Retail Price',
        }
        if not reader.fieldnames or not expected.issubset(reader.fieldnames):
            raise ValueError('Fuel CSV is missing one or more required columns.')
        for record_number, row in enumerate(reader, start=2):
            opis_id = (row['OPIS Truckstop ID'] or '').strip()
            name = (row['Truckstop Name'] or '').strip()
            address = (row['Address'] or '').strip()
            city = (row['City'] or '').strip()
            state = (row['State'] or '').strip().upper()
            rack_id = (row['Rack ID'] or '').strip()
            raw_price = (row['Retail Price'] or '').strip()
            price = None
            try:
                parsed = Decimal(raw_price)
                if parsed.is_finite() and Decimal('0') < parsed <= Decimal('25'):
                    price = parsed.quantize(PRICE_QUANTUM)
            except (InvalidOperation, ValueError):
                pass

            if state not in US_STATES:
                status = 'filtered_non_us'
            elif price is None:
                status = 'invalid_price'
            elif not opis_id or not name or not city or not state:
                status = 'invalid_identity'
            else:
                status = 'valid'

            key = station_key(opis_id, address, city, state) if status == 'valid' else ''
            record = PriceRecord(
                record_number, opis_id, name, address, city, state,
                rack_id, price, status, key,
            )
            records.append(record)
            if status == 'valid':
                grouped[key].append(record)

    stations = []
    for key, rows in sorted(grouped.items()):
        first = rows[0]
        quotes = sorted({row.price for row in rows})
        price = statistics.median(quotes).quantize(PRICE_QUANTUM)
        stations.append(StationInput(
            key, first.opis_id, first.name, first.address, first.city,
            first.state, price, len(quotes) > 1,
            [row.source_row_number for row in rows],
        ))
    return records, stations


def load_city_centers(geonames_zip, wanted):
    """Read only matching US populated places from the GeoNames country dump."""
    centers = defaultdict(list)
    with zipfile.ZipFile(geonames_zip) as archive:
        with archive.open('US.txt') as source:
            reader = csv.reader(io.TextIOWrapper(source, encoding='utf-8'), delimiter='\t')
            for row in reader:
                if len(row) < 15 or row[6] != 'P' or row[8] != 'US':
                    continue
                state = row[10]
                names = {normalized_city(row[1]), normalized_city(row[2])}
                for name in names:
                    key = (state, name)
                    if key in wanted:
                        population = int(row[14] or 0)
                        centers[key].append((float(row[4]), float(row[5]), population))
    return {
        key: sorted(points, key=lambda point: -point[2])[:12]
        for key, points in centers.items()
    }


def _csv_member(archive, suffix):
    matches = [member for member in archive.getmembers() if member.name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f'Expected one {suffix} file in OpenInterstate archive.')
    member = matches[0]
    stream = archive.extractfile(member)
    if stream is None:
        raise ValueError(f'Cannot read {suffix} from OpenInterstate archive.')
    return csv.DictReader(io.TextIOWrapper(stream, encoding='utf-8-sig'))


def normalized_exit(value):
    return re.sub(r'[^0-9A-Z]', '', (value or '').upper())


def load_openinterstate(archive_path):
    exits = defaultdict(list)
    gas_places = {}
    gas_links = defaultdict(list)
    with tarfile.open(archive_path, mode='r:gz') as archive:
        for row in _csv_member(archive, '/csv/corridor_exits.csv'):
            highway = row['interstate_name'].strip().upper()
            number = normalized_exit(row['exit_number'])
            if highway and number:
                exits[(highway, number)].append(ExitPoint(
                    row['exit_id'], float(row['lat']), float(row['lon']),
                ))

        for row in _csv_member(archive, '/csv/places.csv'):
            if row['category'] != 'gas':
                continue
            geometry = json.loads(row['geometry_geojson'])
            longitude, latitude = geometry['coordinates'][:2]
            gas_places[row['place_id']] = GasPlace(
                row['place_id'], row['name'] or row['display_name'],
                row['brand'], float(latitude), float(longitude),
            )

        for row in _csv_member(archive, '/csv/exit_place_links.csv'):
            place_id = row['place_id']
            if place_id in gas_places and float(row['distance_m']) <= 3000:
                gas_links[row['exit_id']].append((place_id, float(row['distance_m'])))

    return exits, gas_places, gas_links


def haversine_miles(lat1, lon1, lat2, lon2):
    earth_radius_miles = 3958.7613
    a1, a2 = math.radians(lat1), math.radians(lat2)
    dlat = a2 - a1
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(a1) * math.cos(a2) * math.sin(dlon / 2) ** 2
    return 2 * earth_radius_miles * math.asin(min(1, math.sqrt(a)))


def _cluster_points(points, within_miles=2.0):
    clusters = []
    for point in points:
        matching = [
            cluster for cluster in clusters
            if any(haversine_miles(point.latitude, point.longitude,
                                   member.latitude, member.longitude) <= within_miles
                   for member in cluster)
        ]
        if matching:
            matching[0].append(point)
            for extra in matching[1:]:
                matching[0].extend(extra)
                clusters.remove(extra)
        else:
            clusters.append([point])
    return clusters


def _brand_tokens(value):
    value = re.sub(r"['’]S\b", 'S', (value or '').upper())
    return {
        token for token in WORD_RE.findall(value)
        if not token.isdigit() and token not in GENERIC_NAME_WORDS
    }


def _matching_gas_place(station, cluster, gas_places, gas_links):
    station_words = _brand_tokens(station.name)
    if not station_words:
        return None, False
    candidates = {}
    for exit_point in cluster:
        for place_id, distance_m in gas_links.get(exit_point.exit_id, []):
            place = gas_places[place_id]
            if distance_m > 2000:
                continue
            place_words = _brand_tokens(place.brand) or _brand_tokens(place.name)
            if place_words and (
                place_words <= station_words or station_words <= place_words
            ):
                candidates[place_id] = place
    if not candidates:
        return None, False
    places = list(candidates.values())
    # Two outlets of the same brand can sit at one interchange. Treat separate
    # POI coordinates as a conflict, even when they are a short walk apart.
    if len(_cluster_points(places, within_miles=0.01)) > 1:
        return None, True
    return sorted(places, key=lambda place: place.place_id)[0], False


def resolve_from_exits(station, city_centers, exits, gas_places, gas_links):
    highway_matches = list(INTERSTATE_RE.finditer(station.address))
    exit_matches = list(EXIT_RE.finditer(station.address))
    if not highway_matches or not exit_matches:
        return {'location_type': 'unresolved', 'location_note': 'No interstate exit to match.'}

    # A record can list two concurrent interstates before one exit, or a
    # separate highway and exit after the first pair. Try only highways that
    # belong to each exit clause, then let the city resolve repeated numbers.
    pairs = set()
    previous_exit_end = 0
    for exit_match in exit_matches:
        before_exit = [
            match for match in highway_matches
            if match.start() < exit_match.start()
        ]
        clause_highways = [
            match for match in before_exit
            if match.start() >= previous_exit_end
        ]
        if not clause_highways and before_exit:
            clause_highways = before_exit[-1:]
        for highway_match in clause_highways:
            pairs.add((
                f'I-{highway_match.group(1).upper()}',
                normalized_exit(exit_match.group(1)),
            ))
        previous_exit_end = exit_match.end()
    points_by_id = {
        point.exit_id: point
        for pair in sorted(pairs) for point in exits.get(pair, [])
    }
    points = [points_by_id[exit_id] for exit_id in sorted(points_by_id)]
    if not points:
        return {'location_type': 'unresolved', 'location_note': 'Exit absent from OpenInterstate.'}
    clusters = _cluster_points(points)
    centers = city_centers.get((station.state, normalized_city(station.city)), [])
    if centers:
        scored = sorted((
            (
                min(haversine_miles(point.latitude, point.longitude, lat, lon)
                    for point in cluster for lat, lon, _ in centers),
                cluster,
            )
            for cluster in clusters
        ), key=lambda item: item[0])
        best_distance, best_cluster = scored[0]
        if best_distance > 40:
            return {'location_type': 'unresolved', 'location_note': 'Exit too far from listed city.'}
        if len(scored) > 1 and scored[1][0] <= best_distance + 10:
            return {'location_type': 'ambiguous', 'location_note': 'Multiple plausible exits.'}
    elif len(clusters) == 1:
        best_cluster = clusters[0]
    else:
        return {'location_type': 'ambiguous', 'location_note': 'City not found; multiple exits.'}

    place, multiple_places = _matching_gas_place(
        station, best_cluster, gas_places, gas_links,
    )
    if multiple_places:
        return {'location_type': 'ambiguous', 'location_note': 'Multiple matching gas POIs.'}
    if place:
        return {
            'location_type': 'station', 'latitude': place.latitude,
            'longitude': place.longitude, 'location_source': 'openinterstate_exit_poi',
            'location_reference': place.place_id,
            'location_note': 'Named gas POI linked to the listed highway exit.',
        }
    first = sorted(best_cluster, key=lambda point: point.exit_id)[0]
    return {
        'location_type': 'exit', 'latitude': first.latitude,
        'longitude': first.longitude, 'location_source': 'openinterstate_exit',
        'location_reference': first.exit_id,
        'location_note': 'Approximate exit access point; station forecourt unverified.',
    }


def build_gas_grid(gas_places):
    grid = defaultdict(list)
    for place in gas_places.values():
        grid[(math.floor(place.latitude * 4), math.floor(place.longitude * 4))].append(place)
    return grid


def resolve_from_nearby_gas(station, city_centers, gas_grid):
    """Audit a town-level POI candidate without asserting station identity."""
    centers = city_centers.get((station.state, normalized_city(station.city)), [])
    station_words = _brand_tokens(station.name)
    if not centers or not station_words:
        return None
    candidates = {}
    for lat, lon, _ in centers:
        grid_lat, grid_lon = math.floor(lat * 4), math.floor(lon * 4)
        for lat_offset in (-1, 0, 1):
            for lon_offset in (-1, 0, 1):
                for place in gas_grid.get((grid_lat + lat_offset, grid_lon + lon_offset), []):
                    if haversine_miles(lat, lon, place.latitude, place.longitude) > 8:
                        continue
                    place_words = _brand_tokens(place.brand) or _brand_tokens(place.name)
                    if place_words and (
                        place_words <= station_words or station_words <= place_words
                    ):
                        candidates[place.place_id] = place
    if not candidates:
        return None
    places = list(candidates.values())
    if len(_cluster_points(places, within_miles=0.1)) > 1:
        return {
            'location_type': 'ambiguous',
            'location_note': 'Multiple named gas POIs near the listed city.',
        }
    place = sorted(places, key=lambda item: item.place_id)[0]
    return {
        'location_type': 'ambiguous', 'location_source': 'openinterstate_city_poi',
        'location_reference': place.place_id,
        'location_note': 'Named gas POI near city lacks a matching road or exit.',
    }
