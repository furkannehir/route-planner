import csv
import io
import json
import tarfile
import tempfile
import zipfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from .geocoding import resolve_geocoded_station
from .importing import (
    ExitPoint, GasPlace, StationInput, build_gas_grid, read_prices,
    resolve_from_exits, resolve_from_nearby_gas,
)
from .models import FuelImportRun, FuelPriceRow, FuelStation
from .planning import (
    Candidate, FuelPlanError, RouteProjector, candidates_along_route,
    optimize_fuel_stops,
)
from .us_boundaries import is_us_location


CSV_COLUMNS = [
    'OPIS Truckstop ID', 'Truckstop Name', 'Address', 'City',
    'State', 'Rack ID', 'Retail Price',
]


def write_prices(path, rows):
    with path.open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)


class ImportRulesTests(SimpleTestCase):
    def test_source_rows_are_audited_and_distinct_prices_use_median(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.csv'
            write_prices(path, [
                ['10', 'PILOT #10', 'I-35 EXIT 271', 'Jarrell', 'TX', '1', '3.00'],
                ['10', 'PILOT #10', 'I-35 EXIT 271', 'Jarrell', 'TX', '1', '5.00'],
                ['10', 'PILOT #10', 'I-35 EXIT 271', 'Jarrell', 'TX', '1', '3.00'],
                ['20', 'CANADIAN STOP', 'I-1 EXIT 1', 'Toronto', 'ON', '2', '4.00'],
                ['30', 'BAD PRICE', 'I-35 EXIT 271', 'Jarrell', 'TX', '3', '-1'],
            ])
            records, stations = read_prices(path)
        self.assertEqual([r.source_row_number for r in records], [2, 3, 4, 5, 6])
        self.assertEqual([r.status for r in records], [
            'valid', 'valid', 'valid', 'filtered_non_us', 'invalid_price',
        ])
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0].price, Decimal('4.00000000'))
        self.assertTrue(stations[0].price_conflict)
        self.assertEqual(stations[0].source_row_numbers, [2, 3, 4])

    def test_exit_match_uses_city_and_named_gas_poi(self):
        station = StationInput(
            'station-1', '10', 'PILOT #10', 'I-35 EXIT 271', 'Jarrell',
            'TX', Decimal('3.00'), False, [2],
        )
        city_centers = {('TX', 'JARRELL'): [(30.82, -97.60, 2000)]}
        exits = {('I-35', '271'): [
            ExitPoint('near', 30.82, -97.60),
            ExitPoint('far', 39.0, -97.60),
        ]}
        place = GasPlace('gas-1', 'Pilot', 'Pilot', 30.821, -97.601)
        location = resolve_from_exits(
            station, city_centers, exits, {'gas-1': place},
            {'near': [('gas-1', 200)]},
        )
        self.assertEqual(location['location_type'], 'station')
        self.assertEqual(location['location_reference'], 'gas-1')

        ambiguous = resolve_from_exits(station, {}, exits, {}, {})
        self.assertEqual(ambiguous['location_type'], 'ambiguous')

    def test_multiple_interstates_and_exits_use_the_matching_clause(self):
        station = StationInput(
            'station-1', '10', 'PILOT #10',
            'I-10, EXIT 246 & I-510, EXIT 2C', 'New Orleans',
            'LA', Decimal('3.00'), False, [2],
        )
        centers = {('LA', 'NEWORLEANS'): [(30.0, -90.0, 1000)]}
        exits = {('I-510', '2C'): [ExitPoint('right', 30.0, -90.0)]}
        location = resolve_from_exits(station, centers, exits, {}, {})
        self.assertEqual(location['location_type'], 'exit')
        self.assertEqual(location['location_reference'], 'right')

        concurrent = StationInput(
            'station-2', '11', 'PILOT #11', 'I-29 & I-80, EXIT 3',
            'Council Bluffs', 'IA', Decimal('3.00'), False, [3],
        )
        centers = {('IA', 'COUNCILBLUFFS'): [(41.26, -95.85, 1000)]}
        exits = {('I-80', '3'): [ExitPoint('concurrent', 41.26, -95.85)]}
        location = resolve_from_exits(concurrent, centers, exits, {}, {})
        self.assertEqual(location['location_reference'], 'concurrent')

    def test_two_nearby_same_brand_pois_are_ambiguous(self):
        station = StationInput(
            'station-1', '10', 'PILOT #10', 'I-35 EXIT 271',
            'Jarrell', 'TX', Decimal('3.00'), False, [2],
        )
        centers = {('TX', 'JARRELL'): [(30.82, -97.60, 2000)]}
        exits = {('I-35', '271'): [ExitPoint('near', 30.82, -97.60)]}
        first = GasPlace('gas-1', 'Pilot', 'Pilot', 30.821, -97.601)
        second = GasPlace('gas-2', 'Pilot', 'Pilot', 30.8215, -97.601)
        location = resolve_from_exits(
            station, centers, exits,
            {'gas-1': first, 'gas-2': second},
            {'near': [('gas-1', 200), ('gas-2', 250)]},
        )
        self.assertEqual(location['location_type'], 'ambiguous')

    def test_city_poi_fallback_is_excluded_without_road_anchor(self):
        station = StationInput(
            'station-1', '10', 'PILOT #10', 'US-190', 'Jarrell',
            'TX', Decimal('3.00'), False, [2],
        )
        city_centers = {('TX', 'JARRELL'): [(30.82, -97.60, 2000)]}
        first = GasPlace('gas-1', 'Pilot', 'Pilot', 30.821, -97.601)
        second = GasPlace('gas-2', 'Pilot', 'Pilot', 30.84, -97.60)
        one = resolve_from_nearby_gas(
            station, city_centers, build_gas_grid({'gas-1': first}),
        )
        self.assertEqual(one['location_type'], 'ambiguous')
        self.assertEqual(one['location_reference'], 'gas-1')
        self.assertNotIn('latitude', one)
        two = resolve_from_nearby_gas(
            station, city_centers,
            build_gas_grid({'gas-1': first, 'gas-2': second}),
        )
        self.assertEqual(two['location_type'], 'ambiguous')

    def test_geocoder_accepts_named_poi_but_not_city_result(self):
        station = StationInput(
            'station-1', '10', 'PILOT #10', 'US-190', 'Jarrell',
            'TX', Decimal('3.00'), False, [2],
        )
        feature = {
            'type': 'Feature',
            'geometry': {'type': 'Point', 'coordinates': [-97.601, 30.821]},
            'properties': {
                'country_code': 'us', 'state_code': 'TX', 'name': 'Pilot #10',
                'result_type': 'amenity', 'rank': {'confidence': 0.95},
                'place_id': 'geo-1',
            },
        }
        centers = {('TX', 'JARRELL'): [(30.82, -97.60, 2000)]}
        self.assertEqual(
            resolve_geocoded_station(station, {'features': [feature]}, centers)
            ['location_type'], 'station',
        )
        feature['properties']['name'] = 'Pilot'
        self.assertIsNone(resolve_geocoded_station(
            station, {'features': [feature]}, centers,
        ))
        feature['properties']['name'] = 'Pilot #10'
        feature['properties']['result_type'] = 'city'
        self.assertIsNone(resolve_geocoded_station(
            station, {'features': [feature]}, centers,
        ))
        feature['properties']['result_type'] = 'amenity'
        feature['properties']['city'] = 'Austin'
        self.assertIsNone(resolve_geocoded_station(
            station, {'features': [feature]}, centers,
        ))
        feature['properties']['city'] = 'Jarrell'
        feature['geometry']['coordinates'] = [-97.601, 31.3]
        self.assertIsNone(resolve_geocoded_station(
            station, {'features': [feature]}, centers,
        ))


class ImportCommandTests(TestCase):
    def test_import_is_repeatable_and_preserves_source_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prices = root / 'prices.csv'
            geonames = root / 'geonames.zip'
            exits = root / 'fixture.tar.gz'
            report = root / 'report.json'
            write_prices(prices, [
                ['10', 'PILOT #10', 'I-35 EXIT 271', 'Jarrell', 'TX', '1', '3.00'],
                ['10', 'PILOT #10', 'I-35 EXIT 271', 'Jarrell', 'TX', '1', '3.20'],
                ['20', 'CANADIAN STOP', 'I-1 EXIT 1', 'Toronto', 'ON', '2', '4.00'],
                ['30', 'OTHER FUEL', 'US-190', 'Jarrell', 'TX', '3', '3.50'],
            ])
            with zipfile.ZipFile(geonames, 'w') as archive:
                archive.writestr('US.txt', '\t'.join([
                    '1', 'Jarrell', 'Jarrell', '', '30.82', '-97.60',
                    'P', 'PPL', 'US', '', 'TX', '', '', '', '2000',
                    '', '', 'America/Chicago', '2026-01-01',
                ]) + '\n')
            members = {
                'corridor_exits.csv': (
                    'exit_id,corridor_id,interstate_name,direction_code,'
                    'sequence_index,exit_number,exit_name,lat,lon,geometry_geojson\n'
                    'near,1,I-35,north,1,271,,30.82,-97.60,\n'
                ),
                'places.csv': (
                    'place_id,category,name,display_name,brand,geometry_geojson\n'
                    'gas-1,gas,Pilot,Pilot,Pilot,'
                    '"{""type"":""Point"",""coordinates"":[-97.601,30.821]}"\n'
                ),
                'exit_place_links.csv': (
                    'exit_id,place_id,category,distance_m,rank\n'
                    'near,gas-1,gas,200,1\n'
                ),
            }
            with tarfile.open(exits, 'w:gz') as archive:
                for name, content in members.items():
                    data = content.encode('utf-8')
                    info = tarfile.TarInfo(f'fixture/csv/{name}')
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))

            command_options = {
                'prices': prices, 'openinterstate': exits,
                'geonames': geonames, 'report_file': report,
            }
            call_command('import_fuel_prices', stdout=io.StringIO(), **command_options)
            unmatched = FuelStation.objects.get(opis_id='30')
            self.assertEqual(unmatched.location_type, 'unresolved')
            unmatched.location_type = 'station'
            unmatched.location_source = 'geoapify'
            unmatched.latitude = 30.83
            unmatched.longitude = -97.61
            unmatched.location_reference = 'geo-30'
            unmatched.save()
            call_command('import_fuel_prices', stdout=io.StringIO(), **command_options)

            self.assertEqual(FuelPriceRow.objects.count(), 4)
            self.assertEqual(FuelStation.objects.count(), 2)
            self.assertEqual(FuelImportRun.objects.count(), 2)
            station = FuelStation.objects.get(opis_id='10')
            self.assertEqual(station.source_row_numbers, [2, 3])
            self.assertEqual(station.price_usd_per_gallon, Decimal('3.10000000'))
            self.assertTrue(station.eligible_for_recommendation)
            self.assertEqual(FuelStation.objects.recommendable().count(), 2)
            self.assertEqual(FuelStation.objects.get(opis_id='30').location_source, 'geoapify')
            self.assertEqual(json.loads(report.read_text())['coverage']
                             ['filtered_non_us_records'], 1)


class RoutePlannerTests(SimpleTestCase):
    def test_us_boundary_excludes_canada_and_includes_noncontiguous_states(self):
        self.assertTrue(is_us_location(40.7128, -74.0060))
        self.assertTrue(is_us_location(21.3099, -157.8581))
        self.assertTrue(is_us_location(61.2181, -149.9003))
        self.assertFalse(is_us_location(43.6532, -79.3832))

    def test_cheaper_station_ahead_avoids_expensive_stop(self):
        def candidate(mile, price, key):
            return Candidate(SimpleNamespace(
                station_key=key, opis_id=key, name=key, address='Road',
                city='Town', state='TX', latitude=30.0, longitude=-97.0,
                location_type='station', location_source='fixture',
                location_note='Exact fixture point.', price_conflict=False,
                source_row_numbers=[2],
                price_usd_per_gallon=Decimal(price),
            ), Decimal(mile), 0.0)

        plan = optimize_fuel_stops(1000, [
            candidate('490', '2', 'cheap-first'),
            candidate('800', '5', 'expensive'),
            candidate('900', '3', 'cheap-last'),
        ])
        self.assertEqual(
            [stop['station_key'] for stop in plan['stops']],
            ['cheap-first', 'cheap-last'],
        )
        self.assertEqual(plan['total_cost_usd'], '101.00')
        self.assertEqual(plan['fuel_purchased_gallons'], 50.0)
        self.assertEqual(plan['ending_fuel_gallons'], 0.0)

    def test_short_trip_uses_starting_fuel_and_gap_fails(self):
        plan = optimize_fuel_stops(100, [])
        self.assertEqual(plan['stops'], [])
        self.assertEqual(plan['total_cost_usd'], '0.00')
        self.assertEqual(plan['fuel_used_gallons'], 10.0)
        with self.assertRaises(FuelPlanError):
            optimize_fuel_stops(600, [])

    def test_empty_starting_tank_prices_the_purchase_at_departure(self):
        station = SimpleNamespace(
            station_key='start', opis_id='10', name='Start Fuel',
            address='Road', city='Town', state='TX',
            latitude=30.0, longitude=-97.0,
            location_type='station', location_source='fixture',
            location_note='Exact fixture point.', price_conflict=False,
            source_row_numbers=[2], price_usd_per_gallon=Decimal('2.00'),
        )
        plan = optimize_fuel_stops(
            100, [Candidate(station, Decimal('0'), 0.0)],
            starting_fuel_gallons=0,
        )
        self.assertEqual(plan['total_cost_usd'], '20.00')
        self.assertEqual(plan['fuel_purchased_gallons'], 10.0)


@override_settings(ORS_API_KEY='test-key')
class RouteEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        FuelImportRun.objects.create(
            source_sha256='0' * 64, exit_release='fixture',
            geonames_sha256='1' * 64, coverage={},
        )

    @staticmethod
    def route_response(distance_miles):
        return {
            'features': [{
                'geometry': {
                    'type': 'LineString',
                    'coordinates': [
                        [-97.6, 30.8], [-97.6, 30.9], [-97.6, 31.0],
                    ],
                },
                'properties': {'summary': {
                    'distance': distance_miles * 1609.344,
                    'duration': distance_miles * 60,
                }},
            }],
        }

    @staticmethod
    def payload():
        return {
            'start': {'latitude': 30.8, 'longitude': -97.6},
            'finish': {'latitude': 31.0, 'longitude': -97.6},
        }

    @staticmethod
    def response_bytes(value):
        return io.BytesIO(json.dumps(value).encode('utf-8'))

    def test_coordinate_route_calls_provider_once_and_reuses_cache(self):
        FuelStation.objects.create(
            station_key='cheap', opis_id='10', name='Cheap Fuel',
            address='I-35 EXIT 100', city='Jarrell', state='TX',
            price_usd_per_gallon=Decimal('2.00'),
            source_row_numbers=[2], latitude=30.895, longitude=-97.6,
            location_type='station', location_source='fixture',
        )
        FuelStation.objects.create(
            station_key='expensive', opis_id='11', name='Expensive Fuel',
            address='I-35 EXIT 200', city='Jarrell', state='TX',
            price_usd_per_gallon=Decimal('5.00'),
            source_row_numbers=[3], latitude=30.96, longitude=-97.6,
            location_type='exit', location_source='fixture',
        )
        FuelStation.objects.create(
            station_key='ambiguous', opis_id='12', name='Uncertain Fuel',
            address='I-35 EXIT 150', city='Jarrell', state='TX',
            price_usd_per_gallon=Decimal('0.01'),
            source_row_numbers=[4], latitude=30.93, longitude=-97.6,
            location_type='ambiguous', location_source='fixture',
        )
        with patch('fuel_routes.routing.urlopen') as urlopen:
            urlopen.return_value = self.response_bytes(self.route_response(1000))
            first = self.client.post(
                '/api/v1/routes/', data=json.dumps(self.payload()),
                content_type='application/json',
            )
            second = self.client.post(
                '/api/v1/routes/', data=json.dumps(self.payload()),
                content_type='application/json',
            )
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(urlopen.call_count, 1)
        provider_request = urlopen.call_args.args[0]
        self.assertEqual(provider_request.get_method(), 'POST')
        self.assertIn('/openrouteservice/v2/directions/driving-car/geojson',
                      provider_request.full_url)
        self.assertEqual(json.loads(provider_request.data)['options'],
                         {'avoid_borders': 'all'})
        self.assertEqual(first.json()['provider_requests'], {
            'directions': 1, 'geocoding': 0,
        })
        self.assertEqual(second.json()['provider_requests'], {
            'directions': 0, 'geocoding': 0,
        })
        self.assertEqual(first.json()['map']['type'], 'FeatureCollection')
        self.assertEqual(len(first.json()['fuel_stops']), 2)
        self.assertNotIn('ambiguous', [
            stop['station_key'] for stop in first.json()['fuel_stops']
        ])
        self.assertEqual(len(first.json()['map']['features']), 5)

    def test_text_locations_use_two_geocodes_and_one_route(self):
        def provider(request, timeout):
            if '/pelias/v1/search' in request.full_url:
                if 'Austin' in request.full_url:
                    point = [-97.6, 30.8]
                else:
                    point = [-97.6, 31.0]
                return self.response_bytes({'features': [{
                    'geometry': {'coordinates': point},
                    'properties': {
                        'country_a': 'USA', 'confidence': 0.99,
                        'label': 'US test location',
                    },
                }]})
            return self.response_bytes(self.route_response(20))

        with patch('fuel_routes.routing.urlopen', side_effect=provider) as urlopen:
            response = self.client.post(
                '/api/v1/routes/', data=json.dumps({
                    'start': 'Austin, TX', 'finish': 'Jarrell, TX',
                }), content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(response.json()['provider_requests'], {
            'directions': 1, 'geocoding': 2,
        })

    def test_canadian_coordinate_is_rejected_without_provider_call(self):
        payload = self.payload()
        payload['start'] = {'latitude': 43.6532, 'longitude': -79.3832}
        with patch('fuel_routes.routing.urlopen') as urlopen:
            response = self.client.post(
                '/api/v1/routes/', data=json.dumps(payload),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(urlopen.call_count, 0)

    def test_unreachable_route_returns_explicit_error(self):
        with patch('fuel_routes.routing.urlopen') as urlopen:
            urlopen.return_value = self.response_bytes(self.route_response(600))
            response = self.client.post(
                '/api/v1/routes/', data=json.dumps(self.payload()),
                content_type='application/json',
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()['error']['code'], 'insufficient_fuel_coverage',
        )
