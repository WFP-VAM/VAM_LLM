"""Offline cartography, identity and final display-space map contracts."""
from copy import deepcopy
from itertools import combinations
import gzip
import json
from pathlib import Path
import socket

import pytest
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from app.services.mfi_drafter import map_basemap as basemap
from app.services.mfi_drafter.map_rendering import map_data, draw_map, viewport, _paths, MAP_WIDTH
from app.services.mfi_drafter.render_worker import render, RenderWorker
from app.services.mfi_drafter.reliable_contracts import fingerprint


def state(country='Benin', count=15, lon=2.3, lat=6.5):
    rows = [{'market_key': f'id-{i}', 'market_name': f'Marché {i}', 'overall_mfi': i % 11,
             'latitude': lat + i * .00001, 'longitude': lon + i * .00001} for i in range(count)]
    profiles = [{**m, 'is_priority_market': True, 'selection_order': i + 1, 'score_rank': i + 1}
                for i, m in enumerate(rows)]
    return {'country': country, 'markets_data': rows, 'assessment_profile': {'markets': profiles}}


def draw(data):
    fig = Figure(dpi=150)
    FigureCanvasAgg(fig)
    try:
        metadata = draw_map(fig, data)
        fig.canvas.draw()
        return fig, metadata
    except Exception:
        fig.clear()
        raise


def test_pinned_asset_integrity_and_holes():
    manifest, features = basemap.load_basemap()
    assert manifest['version'] == '5.1.1' and len(features) == 258
    assert any('BEN' in f['properties']['codes'] for f in features)
    assert any('HTI' in f['properties']['codes'] for f in features)
    polygons = [p for f in features for p in ([f['geometry']['coordinates']] if f['geometry']['type'] == 'Polygon' else f['geometry']['coordinates'])]
    assert any(len(p) > 1 for p in polygons)
    assert 'countries.geojson.gz' not in Path('.dockerignore').read_text()
    assert 'COPY . /app' in Path('Dockerfile').read_text()


def test_preflight_does_not_keep_country_geometries_in_main_process():
    basemap._load.cache_clear()
    basemap._verified.cache_clear()
    basemap.preflight_maps(state(count=1))
    assert basemap._load.cache_info().currsize == 0
    assert basemap._verified.cache_info().currsize == 1


@pytest.mark.parametrize('defect', ['missing', 'corrupt', 'malformed'])
def test_bad_asset_fails_before_model_invocation(tmp_path, monkeypatch, defect):
    from app.services.mfi_drafter import light_graph, light_service
    from app.services.mfi_drafter.errors import MFIRunError
    from app.services.mfi_drafter.schemas import MFIReleaseControl
    raw = (basemap.ASSET_DIRECTORY / 'countries.geojson.gz').read_bytes()
    manifest = json.loads((basemap.ASSET_DIRECTORY / 'manifest.json').read_text())
    if defect == 'malformed':
        import hashlib
        raw = gzip.compress(b'{"type":"FeatureCollection","features":[]}')
        manifest['asset_sha256'] = hashlib.sha256(raw).hexdigest()
    if defect != 'missing': (tmp_path / 'countries.geojson.gz').write_bytes(raw if defect != 'corrupt' else b'corrupt')
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(basemap, 'ASSET_DIRECTORY', tmp_path)
    monkeypatch.setattr(light_graph, 'build_graph', lambda *a, **k: pytest.fail('Preflight should precede execution'))
    data = state()
    with pytest.raises(MFIRunError, match='cartography') as error:
        light_service.validate_submission(data)
    assert error.value.status_code == 503
    with pytest.raises(basemap.MFICartographyError):
        light_service.run_mfi_report_generation(country='Benin', markets=[], csv_data=data,
            data_collection_start='2026-01-01', data_collection_end='2026-01-02',
            release_control=MFIReleaseControl(analysis_version='2', enabled=True, configuration_status='configured'))


@pytest.mark.parametrize('country,lon,lat', [('Benin',2.3,6.5), ('Haiti',-72.3,18.5), ('Madagascar',47,-19), ('Testland',2.3,6.5)])
def test_map_background_offline_and_layout(country, lon, lat, monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('Offline rendering attempted network'))
    data = map_data(state(country, lon=lon, lat=lat))
    fig, meta = draw(data)
    try:
        assert meta['background_countries']
        assert bool(meta['highlighted_countries']) == (country != 'Testland')
        assert bool(meta['limitations']) == (country == 'Testland')
        assert len(meta['callouts']) == 15
        assert meta['values'] == [i % 11 for i in range(15)]
        for a, b in combinations(meta['callouts'], 2):
            x0,y0,x1,y1 = a['bbox_pixels']; u0,v0,u1,v1 = b['bbox_pixels']
            assert x1 <= u0 or u1 <= x0 or y1 <= v0 or v1 <= y0
        assert meta['legend_font_points'] * MAP_WIDTH / meta['figure_width_inches'] >= 8
        # Actual final numbered text bounds, not only estimated placement boxes.
        boxes = [a.get_bbox_patch().get_window_extent(fig.canvas.get_renderer()) for a in fig.axes[0].texts if a.get_bbox_patch()]
        assert all(not a.overlaps(b) for a,b in combinations(boxes,2))
        for text in fig.axes[1].texts:
            box = text.get_window_extent(fig.canvas.get_renderer())
            assert box.x0 >= 0 and box.y0 >= 0 and box.x1 <= fig.bbox.x1 and box.y1 <= fig.bbox.y1
        credit = fig.texts[-1].get_window_extent(fig.canvas.get_renderer())
        colour_label = fig.axes[2].xaxis.label.get_window_extent(fig.canvas.get_renderer())
        assert not credit.overlaps(colour_label)
    finally:
        fig.clear()


def test_identity_coincident_points_missing_coordinates_and_long_names():
    s = state()
    for m in s['markets_data']:
        m.update(latitude=6.5, longitude=2.3, market_name='Shared name')
    s['markets_data'][1]['latitude'] = None
    s['assessment_profile']['markets'][3]['market_name'] = 'São José — Marché central de la région orientale'
    data = map_data(s)
    fig, meta = draw(data)
    try:
        assert [p['number'] for p in meta['callouts']] == [1, *range(3,16)]
        assert [p['number'] for p in meta['legend']] == list(range(1,16))
        assert not meta['legend'][1]['located']
        assert len(set(p['market_key'] for p in meta['callouts'])) == 14
        assert all(p['longitude'] == 2.3 and p['latitude'] == 6.5 for p in meta['source_coordinates'])
        assert meta['located_market_count'] == 14
        # Reordering raw input cannot change identity joins or placement.
        s['markets_data'].reverse()
        fig2, meta2 = draw(map_data(s))
        try: assert meta2['callouts'] == meta['callouts']
        finally: fig2.clear()
    finally:
        fig.clear()


def test_antimeridian_and_preserved_hole_path():
    from matplotlib.path import Path as MPath
    data = state('Fiji', 2, 179.8, -17)
    data['markets_data'][1]['longitude'] = -179.8
    packet = map_data(data)
    xs, bounds = viewport(packet['markets'])
    assert bounds[1] - bounds[0] < 1
    fig, meta = draw(packet)
    try:
        assert 'Fiji' in meta['highlighted_countries']
        assert meta['source_coordinates'][1]['longitude'] == -179.8
        assert max(xs) - min(xs) == pytest.approx(.4)
    finally: fig.clear()
    geometry = {'type': 'Polygon', 'coordinates': [[[0,0],[4,0],[4,4],[0,4],[0,0]], [[1,1],[1,3],[3,3],[3,1],[1,1]]]}
    paths = list(_paths(geometry, (-1,5,-1,5)))
    assert len(paths) == 1 and list(paths[0].codes).count(MPath.MOVETO) == 2


def test_map_job_rejects_wrong_asset_version():
    data = map_data(state(count=1))
    data['cartography']['renderer_version'] = 'old'
    job = {'run_id':'wrong','figure_id':'geographic_map','kind':'map','analytical_fingerprint':fingerprint(data),'data':data}
    with pytest.raises(basemap.MFICartographyError, match='versions differ'):
        render(job)


def test_invalid_coordinates_are_disclosed_and_not_serialized_as_nonfinite():
    s = state(count=3)
    s['markets_data'][0]['latitude'] = float('nan')
    s['markets_data'][1]['longitude'] = 200
    data = map_data(s)
    assert fingerprint(data)
    fig, meta = draw(data)
    try:
        assert meta['located_market_count'] == 1
        assert [p['number'] for p in meta['callouts']] == [3]
        assert [p['located'] for p in meta['legend']] == [False,False,True]
    finally: fig.clear()


def test_source_identity_conflicts_are_not_resolved_by_first_match():
    s = state(count=2)
    s['markets_data'][1]['market_key'] = s['markets_data'][0]['market_key']
    with pytest.raises(basemap.MFICartographyError, match='Duplicate stable identity'):
        map_data(s)


def test_concurrent_map_workers_are_isolated():
    from concurrent.futures import ThreadPoolExecutor
    def work(country, lon, lat):
        data = map_data(state(country, 2, lon, lat))
        job = {'run_id':country,'figure_id':'geographic_map','kind':'map','analytical_fingerprint':fingerprint(data),'data':data}
        worker = RenderWorker()
        try:
            result = worker.run(job)
            assert result['run_id'] == country
            assert result['analytical_fingerprint'] == fingerprint(data)
            assert result['metadata']['source_coordinates'][0]['longitude'] == lon
            return result
        finally: worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        b = pool.submit(work,'Benin',2.3,6.5); h = pool.submit(work,'Haiti',-72.3,18.5)
        assert b.result()['image'] != h.result()['image']
