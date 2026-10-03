import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from fuel_routes.geocoding import (
    GeoapifyGeocoder, GeocodingError, read_geocode_cache,
    resolve_geocoded_station,
)
from fuel_routes.importing import (
    build_gas_grid, file_sha256, load_city_centers, load_openinterstate,
    normalized_city, read_prices, resolve_from_exits, resolve_from_nearby_gas,
)
from fuel_routes.models import FuelImportRun, FuelPriceRow, FuelStation


DEFAULT_EXIT_ARCHIVE = 'openinterstate-release-2026-07-13-gha-33.tar.gz'
DEFAULT_EXIT_SHA256 = '36fd3c2bddb94a11bfc5f6b178e866f341f8a6e779c1bfd4a94def564a829487'


class Command(BaseCommand):
    help = 'Import, deduplicate, and locate the supplied US fuel stations once.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--prices', type=Path,
            default=settings.BASE_DIR / 'fuel-prices-for-be-assessment.csv',
        )
        parser.add_argument(
            '--openinterstate', type=Path,
            default=settings.BASE_DIR / '.cache' / DEFAULT_EXIT_ARCHIVE,
        )
        parser.add_argument(
            '--geonames', type=Path,
            default=settings.BASE_DIR / '.cache' / 'geonames-US.zip',
        )
        parser.add_argument('--geocode', action='store_true')
        parser.add_argument('--max-geocode-requests', type=int, default=2500)
        parser.add_argument(
            '--geocode-cache', type=Path,
            default=settings.BASE_DIR / '.cache' / 'geoapify-results.jsonl',
        )
        parser.add_argument(
            '--report-file', type=Path,
            default=settings.BASE_DIR / 'import-coverage.json',
        )

    def handle(self, *args, **options):
        prices_path = options['prices']
        exit_path = options['openinterstate']
        geonames_path = options['geonames']
        for path in (prices_path, exit_path, geonames_path):
            if not path.is_file():
                raise CommandError(f'Missing import input: {path}')
        if options['max_geocode_requests'] < 0:
            raise CommandError('--max-geocode-requests must be nonnegative.')

        source_sha = file_sha256(prices_path)
        exit_sha = file_sha256(exit_path)
        geonames_sha = file_sha256(geonames_path)
        if exit_path.name == DEFAULT_EXIT_ARCHIVE and exit_sha != DEFAULT_EXIT_SHA256:
            raise CommandError('OpenInterstate archive does not match the pinned SHA-256.')

        try:
            price_rows, stations = read_prices(prices_path)
            wanted_cities = {
                (station.state, normalized_city(station.city)) for station in stations
            }
            city_centers = load_city_centers(geonames_path, wanted_cities)
            exits, gas_places, gas_links = load_openinterstate(exit_path)
            gas_grid = build_gas_grid(gas_places)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise CommandError(f'Import input is invalid: {error}') from error

        geocoder = None
        cached_geocodes = read_geocode_cache(options['geocode_cache'])
        previous_geocodes = {
            station.station_key: station
            for station in FuelStation.objects.filter(location_source='geoapify')
        }
        if options['geocode']:
            api_key = os.environ.get('GEOAPIFY_API_KEY', '')
            if not api_key:
                raise CommandError('Set GEOAPIFY_API_KEY before using --geocode.')
            geocoder = GeoapifyGeocoder(
                api_key, options['geocode_cache'], options['max_geocode_requests'],
            )

        station_models = []
        pending_geocodes = []
        for station in stations:
            location = resolve_from_exits(
                station, city_centers, exits, gas_places, gas_links,
            )
            if location['location_type'] == 'unresolved':
                nearby = resolve_from_nearby_gas(station, city_centers, gas_grid)
                if nearby:
                    location = nearby
            if location['location_type'] in {'unresolved', 'ambiguous'}:
                response = cached_geocodes.get(station.station_key)
                if response is None and geocoder:
                    pending_geocodes.append(station)
                geocoded = resolve_geocoded_station(station, response, city_centers)
                if geocoded:
                    location = geocoded
                elif station.station_key in previous_geocodes:
                    previous = previous_geocodes[station.station_key]
                    location = {
                        'location_type': 'station',
                        'latitude': previous.latitude,
                        'longitude': previous.longitude,
                        'location_source': 'geoapify',
                        'location_reference': previous.location_reference,
                        'location_note': previous.location_note,
                    }
            station_models.append(FuelStation(
                station_key=station.station_key,
                opis_id=station.opis_id,
                name=station.name,
                address=station.address,
                city=station.city,
                state=station.state,
                price_usd_per_gallon=station.price,
                price_conflict=station.price_conflict,
                source_row_numbers=station.source_row_numbers,
                latitude=location.get('latitude'),
                longitude=location.get('longitude'),
                location_type=location['location_type'],
                location_source=location.get('location_source', ''),
                location_reference=location.get('location_reference', ''),
                location_note=location.get('location_note', ''),
            ))

        if geocoder and pending_geocodes:
            remaining = max(0, geocoder.max_requests - geocoder.requests_made_today)
            selected = pending_geocodes[:remaining]
            models_by_key = {model.station_key: model for model in station_models}
            # Request starts are spaced by the geocoder's shared rate limiter.
            with ThreadPoolExecutor(max_workers=24) as pool:
                try:
                    for station, response in zip(
                        selected, pool.map(geocoder.lookup, selected),
                    ):
                        location = resolve_geocoded_station(
                            station, response, city_centers,
                        )
                        if location:
                            model = models_by_key[station.station_key]
                            model.location_type = location['location_type']
                            model.latitude = location.get('latitude')
                            model.longitude = location.get('longitude')
                            model.location_source = location.get('location_source', '')
                            model.location_reference = location.get(
                                'location_reference', '',
                            )
                            model.location_note = location.get('location_note', '')
                except GeocodingError as error:
                    raise CommandError(str(error)) from error

        row_counts = Counter(row.status for row in price_rows)
        location_counts = Counter(station.location_type for station in station_models)
        source_counts = Counter(station.location_source for station in station_models)
        excluded_reasons = Counter(
            station.location_note for station in station_models
            if station.location_type in {'ambiguous', 'unresolved'}
        )
        eligible = location_counts['station'] + location_counts['exit']
        coverage = {
            'csv_records': len(price_rows),
            'valid_us_price_records': row_counts['valid'],
            'filtered_non_us_records': row_counts['filtered_non_us'],
            'invalid_price_records': row_counts['invalid_price'],
            'invalid_identity_records': row_counts['invalid_identity'],
            'unique_us_stations': len(station_models),
            'conflicting_price_stations': sum(s.price_conflict for s in station_models),
            'city_names_located': len(city_centers),
            'city_names_missing': len(wanted_cities) - len(city_centers),
            'exact_station_locations': location_counts['station'],
            'station_pois_linked_to_exits': source_counts['openinterstate_exit_poi'],
            'city_poi_candidates_excluded': source_counts['openinterstate_city_poi'],
            'stations_matched_by_geocoder': source_counts['geoapify'],
            'approximate_exit_locations': location_counts['exit'],
            'ambiguous_locations': location_counts['ambiguous'],
            'unresolved_locations': location_counts['unresolved'],
            'recommendable_stations': eligible,
            'recommendable_percent': round(100 * eligible / len(station_models), 1)
            if station_models else 0,
            'stations_without_recommendable_location': (
                location_counts['ambiguous'] + location_counts['unresolved']
            ),
            'excluded_by_reason': dict(sorted(excluded_reasons.items())),
            'geocoder_requests': geocoder.requests_made if geocoder else 0,
            'geocoder_request_failures': (
                geocoder.request_failures if geocoder else 0
            ),
            'geocoder_requests_today': (
                geocoder.requests_made_today if geocoder else 0
            ),
            'cached_geocode_responses': (
                len(geocoder.cache) if geocoder else len(cached_geocodes)
            ),
            'geocoder_enabled': bool(geocoder),
        }

        raw_models = [FuelPriceRow(
            source_row_number=row.source_row_number,
            opis_id=row.opis_id,
            station_name=row.name,
            address=row.address,
            city=row.city,
            state=row.state,
            rack_id=row.rack_id,
            retail_price=row.price,
            status=row.status,
            station_key=row.station_key,
        ) for row in price_rows]

        with transaction.atomic():
            FuelPriceRow.objects.all().delete()
            FuelStation.objects.all().delete()
            FuelPriceRow.objects.bulk_create(raw_models, batch_size=1000)
            FuelStation.objects.bulk_create(station_models, batch_size=1000)
            FuelImportRun.objects.create(
                source_sha256=source_sha,
                exit_release=exit_path.name,
                geonames_sha256=geonames_sha,
                coverage=coverage,
            )

        report = {
            'sources': {
                'fuel_prices_sha256': source_sha,
                'openinterstate_release': exit_path.name,
                'openinterstate_sha256': exit_sha,
                'geonames_us_sha256': geonames_sha,
            },
            'coverage': coverage,
            'location_policy': (
                'Only exact station POIs and unambiguous interstate exits are '
                'recommendable. Exit coordinates approximate access, not the '
                'station forecourt. Ambiguous and unresolved records are excluded.'
            ),
            'price_policy': (
                'The median of distinct listed prices for the same OPIS ID, '
                'address, city, and state is used. Conflicts are flagged because '
                'the CSV has no observation timestamps.'
            ),
        }
        report_path = options['report_file']
        if report_path:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2) + '\n', encoding='utf-8',
            )
        self.stdout.write(json.dumps(coverage, indent=2))
