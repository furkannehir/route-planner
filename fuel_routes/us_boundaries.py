"""Validate points against the Census 2025 state cartographic boundaries."""

import xml.etree.ElementTree as ET
import zipfile
from functools import lru_cache

from django.conf import settings

from .importing import US_STATES


NS = {'k': 'http://www.opengis.net/kml/2.2'}
BOUNDARY_FILE = 'cb_2025_us_state_500k.zip'


def _ring(text):
    return [
        (float(parts[0]), float(parts[1]))
        for token in text.split()
        if len(parts := token.split(',')) >= 2
    ]


def _contains(ring, longitude, latitude):
    inside = False
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        if (current_y > latitude) != (previous_y > latitude):
            edge_x = (previous_x - current_x) * (latitude - current_y) / (
                previous_y - current_y
            ) + current_x
            if longitude < edge_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


@lru_cache(maxsize=1)
def state_polygons():
    archive_path = settings.BASE_DIR / 'data' / BOUNDARY_FILE
    with zipfile.ZipFile(archive_path) as archive:
        member = next(name for name in archive.namelist() if name.endswith('.kml'))
        root = ET.fromstring(archive.read(member))

    polygons = []
    for placemark in root.findall('.//k:Placemark', NS):
        state = next((
            field.text for field in placemark.findall('.//k:SimpleData', NS)
            if field.get('name') == 'STUSPS'
        ), None)
        if state not in US_STATES:
            continue
        for polygon in placemark.findall('.//k:Polygon', NS):
            outer_text = polygon.findtext(
                'k:outerBoundaryIs/k:LinearRing/k:coordinates', namespaces=NS,
            )
            if not outer_text:
                continue
            outer = _ring(outer_text)
            if len(outer) < 3:
                continue
            holes = [
                _ring(item.text)
                for item in polygon.findall(
                    'k:innerBoundaryIs/k:LinearRing/k:coordinates', NS,
                ) if item.text
            ]
            xs = [point[0] for point in outer]
            ys = [point[1] for point in outer]
            polygons.append((
                (min(xs), min(ys), max(xs), max(ys)), outer, holes,
            ))
    return polygons


def is_us_location(latitude, longitude):
    """Include states and DC, excluding territories and neighboring countries."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return False
    for (min_x, min_y, max_x, max_y), outer, holes in state_polygons():
        if min_x <= longitude <= max_x and min_y <= latitude <= max_y:
            if _contains(outer, longitude, latitude) and not any(
                _contains(hole, longitude, latitude) for hole in holes
            ):
                return True
    return False
