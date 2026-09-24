"""Verified offline cartography shared by preflight and the isolated map worker."""
from functools import lru_cache
import gzip
import hashlib
import json
import math
from pathlib import Path

ASSET_DIRECTORY = Path(__file__).with_name('assets') / 'basemap'
MAP_RENDERER_VERSION = 'mfi-map-v2'


class MFICartographyError(ValueError):
    """A required local cartographic resource is unavailable or invalid."""


def _read(directory):
    try:
        root = Path(directory)
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        raw = (root / 'countries.geojson.gz').read_bytes()
        if hashlib.sha256(raw).hexdigest() != manifest['asset_sha256']:
            raise ValueError('Natural Earth asset checksum mismatch')
        payload = json.loads(gzip.decompress(raw))
        if manifest['version'] != '5.1.1' or payload['type'] != 'FeatureCollection':
            raise ValueError('Unsupported Natural Earth asset version or structure')
        features = payload['features']
        if not features or len(features) != manifest['feature_count']:
            raise ValueError('Incomplete Natural Earth features')
        for feature in features:
            geometry = feature['geometry']
            if geometry['type'] not in ('Polygon', 'MultiPolygon'):
                raise ValueError('Country geometry must be Polygon or MultiPolygon')
            polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
            if not polygons or not isinstance(feature['properties']['codes'], list):
                raise ValueError('Missing country geometry or identifiers')
            for polygon in polygons:
                if not polygon:
                    raise ValueError('Empty country polygon')
                for ring in polygon:
                    if len(ring) < 4 or ring[0] != ring[-1]:
                        raise ValueError('Unclosed country ring')
                    if any(len(p) != 2 or not all(math.isfinite(v) for v in p)
                           or not -180 <= p[0] <= 180 or not -90 <= p[1] <= 90 for p in ring):
                        raise ValueError('Invalid country coordinates')
        return manifest, features
    except Exception as exc:
        raise MFICartographyError(f'MFI offline cartography is unavailable or corrupt: {exc}') from exc


def _resource_key():
    try:
        def stamp(name):
            s = (ASSET_DIRECTORY / name).stat()
            return (s.st_size, s.st_mtime_ns)
        return str(ASSET_DIRECTORY), stamp('countries.geojson.gz'), stamp('manifest.json')
    except MFICartographyError:
        raise
    except Exception as exc:
        raise MFICartographyError(f'MFI offline cartography is unavailable or corrupt: {exc}') from exc


@lru_cache(maxsize=2)
def _load(directory, asset_stamp, manifest_stamp):
    return _read(directory)


@lru_cache(maxsize=2)
def _verified(directory, asset_stamp, manifest_stamp):
    # Preflight retains only the small manifest. The worker owns the geometry cache.
    return _read(directory)[0]


def verified_manifest():
    return _verified(*_resource_key())


def load_basemap():
    return _load(*_resource_key())


def map_contract(manifest=None):
    manifest = manifest if manifest is not None else verified_manifest()
    return {'renderer_version': MAP_RENDERER_VERSION, 'basemap_version': manifest['version'],
            'basemap_sha256': manifest['asset_sha256']}


def valid_location(market):
    try:
        lat, lon = float(market['latitude']), float(market['longitude'])
        return math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180
    except (KeyError, ValueError, TypeError):
        return False


def preflight_maps(loaded):
    # No map is requested if no assessed markets can be located.
    if loaded is None or any(valid_location(m) for m in loaded.get('markets_data', [])):
        verified_manifest()
