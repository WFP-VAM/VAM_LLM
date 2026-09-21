"""Batch upload UX against the real service, without cloud calls or inference."""
import io
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import streamlit as st
from PIL import Image
from streamlit.testing.v1 import AppTest

from app.services.seasonal_outlook import api
from app.services.seasonal_outlook.config import Settings
from app.services.seasonal_outlook.service import Service
from app.services.seasonal_outlook.storage import MemoryStore
from app.services.seasonal_outlook.upload_ui import pending_maps


def image_file(name, color='white'):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
    data = buffer.getvalue()
    return SimpleNamespace(name=name, type='image/png', size=len(data), getvalue=lambda: data)


class UploadBackend:
    def __init__(self):
        settings = Settings(enabled=True, project='company-test', bucket='company-test',
            job='seasonal', job_region='europe-west1', signer='worker@company-test.iam.gserviceaccount.com')
        self.jobs = []
        self.service = Service(settings, MemoryStore(), SimpleNamespace(launch=lambda *args: self.jobs.append(args)))
        self.run = self.service.create(dict(request_id=uuid.uuid4().hex, expected_revision=0,
            region_id='eastern_africa_yemen', report_date='2026-09-16'))
        self.files, self.requests, self.upload_calls = [], [], []
        self.widget_key, self.widget_options = None, None
        self.failure = None

    def uploader(self, label, **kwargs):
        if self.widget_key and self.widget_key != kwargs['key']:
            self.files = []
        self.widget_key, self.widget_options = kwargs['key'], kwargs
        return self.files

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))
        upload = None
        is_upload = method == 'POST' and path.endswith('/maps')
        if is_upload:
            self.upload_calls.append(kwargs)
            filename, content, _ = kwargs['files']['file']
            upload = SimpleNamespace(filename=filename, content=content)
            if len(self.upload_calls) == 2:
                if self.failure == 'before':
                    raise TimeoutError('Connection interrupted before persistence')
                if self.failure == 'concurrent':
                    self.add_saved(image_file('another-analyst.png', 'green'))
        reply = api.handle(method, path.split('/')[2:],
            kwargs.get('json_body', kwargs.get('data')), kwargs.get('params'), upload)
        if reply.status >= 400:
            raise RuntimeError(reply.data)
        if is_upload and len(self.upload_calls) == 2 and self.failure == 'after':
            raise TimeoutError('Acknowledgement lost after persistence')
        return reply.data if reply.data is not None else reply.content

    def add_saved(self, file):
        run = self.current()
        return self.service.upload(run['id'], dict(request_id=uuid.uuid4().hex,
            expected_revision=run['revision'], product='other'), file.getvalue(), file.name)

    def current(self):
        return self.service.get(self.run['id'])


@pytest.fixture
def ui(monkeypatch):
    backend = UploadBackend()
    monkeypatch.setattr(api, 'get_service', lambda: backend.service)
    monkeypatch.setattr(api, 'service_info', backend.service.info)
    monkeypatch.setattr(st, 'file_uploader', backend.uploader)
    shared = ModuleType('streamlit_shared')
    for name in ('apply_wfp_theme', 'render_wfp_sidebar_logo', 'render_onboarding_sidebar_button',
                 'render_instructions_sidebar_button', 'render_bug_report_sidebar_link', 'render_bug_report_header_link'):
        setattr(shared, name, lambda *a, **k: None)
    shared.request_json = shared.request_bytes = backend.request
    shared.safe_show_error = lambda exc: st.error(str(exc))
    monkeypatch.setitem(sys.modules, 'streamlit_shared', shared)
    page = Path(__file__).parents[1] / 'pages/5_Seasonal_Outlook_Drafter.py'
    app = AppTest.from_file(str(page))
    app.query_params['seasonal_run'] = backend.run['id']
    return app, backend


def button(app, label):
    return next(b for b in app.button if b.label == label)


def test_all_maps_saved_with_one_click_and_optional_metadata(ui):
    app, backend = ui
    backend.files = [image_file('rain.png'), image_file('temperature.png', 'red'),
                     image_file('01_observed__rain.png', 'blue')]
    app.run(timeout=20)
    assert not app.exception
    assert backend.widget_options['accept_multiple_files'] is True
    assert backend.widget_options['max_upload_size'] == 30
    details = [e for e in app.expander if 'optional details' in e.label]
    assert len(details) == 3 and all(not e.proto.expanded for e in details)
    assert [s.value for s in app.selectbox if s.label == 'Product category (optional)'] == ['other', 'other', 'observed']
    app.run(timeout=20)
    assert all(method == 'GET' for method, _, _ in backend.requests)
    assert button(app, 'Extract and review evidence').disabled
    # Distinct controls retain analyst overrides for each selected image.
    next(s for s in app.selectbox if s.label == 'Product category (optional)').select('mixed')
    next(t for t in app.text_area if t.label == 'Map note (optional)').input('Analyst coverage note')
    button(app, 'Save selected maps').click().run(timeout=20)
    assert not app.exception and not app.error
    saved = backend.current()['maps']
    assert [m['name'] for m in saved] == ['rain.png', 'temperature.png', '01_observed__rain.png']
    assert [m['product'] for m in saved] == ['mixed', 'other', 'observed']
    assert saved[0]['note'] == 'Analyst coverage note'
    assert all(m['issue_date'] is None for m in saved)
    assert [c['data']['expected_revision'] for c in backend.upload_calls] == [1, 2, 3]
    assert not backend.jobs
    assert not backend.files
    assert not button(app, 'Extract and review evidence').disabled
    assert button(app, 'Save selected maps').disabled
    app.run(timeout=20)
    assert len(backend.upload_calls) == 3


@pytest.mark.parametrize('failure, saved_before_retry', [('before', 1), ('after', 2), ('concurrent', 2)])
def test_interrupted_batch_retains_selection_and_only_retries_missing_maps(ui, failure, saved_before_retry):
    app, backend = ui
    backend.files = [image_file('one.png'), image_file('two.png', 'red'), image_file('three.png', 'blue')]
    backend.failure = failure
    app.run(timeout=20)
    selection_key = backend.widget_key
    [t for t in app.text_area if t.label == 'Map note (optional)'][-1].input('Keep this note after interruption')
    button(app, 'Save selected maps').click().run(timeout=20)
    assert not app.exception
    assert any('Upload stopped' in e.value for e in app.error)
    assert len(backend.current()['maps']) == saved_before_retry
    assert len(backend.upload_calls) == 2  # No automatic retry after uncertainty or conflict.
    assert backend.widget_key == selection_key and len(backend.files) == 3
    assert button(app, 'Extract and review evidence').disabled
    button(app, 'Save selected maps').click().run(timeout=20)
    assert not app.exception and not app.error
    maps = backend.current()['maps']
    assert len(maps) == (4 if failure == 'concurrent' else 3)
    assert next(m for m in maps if m['name'] == 'three.png')['note'] == 'Keep this note after interruption'
    assert len({m['object']['sha256'] for m in maps}) == len(maps)
    uploaded_names = [c['files']['file'][0] for c in backend.upload_calls]
    assert uploaded_names.count('one.png') == 1
    assert uploaded_names.count('two.png') == (1 if failure == 'after' else 2)
    assert not backend.jobs


@pytest.mark.parametrize('invalid_selection', ['duplicate', 'corrupt', 'too_many', 'animated', 'pixels'])
def test_invalid_batch_never_partially_saves_or_starts_extraction(ui, invalid_selection):
    app, backend = ui
    backend.add_saved(image_file('saved.png', 'green'))
    backend.files = [image_file('valid.png')]
    if invalid_selection == 'duplicate':
        backend.files.append(image_file('same-content.png'))
    elif invalid_selection == 'corrupt':
        backend.files.append(SimpleNamespace(name='corrupt.png', type='image/png', getvalue=lambda: b'bad image'))
    elif invalid_selection == 'too_many':
        backend.files = [image_file(f'{i}.png', (i, 0, 0)) for i in range(12)]
    else:
        buffer = io.BytesIO()
        if invalid_selection == 'animated':
            Image.new('RGB', (8, 8), 'red').save(buffer, format='PNG', save_all=True,
                append_images=[Image.new('RGB', (8, 8), 'blue')])
        else:
            Image.new('1', (7000, 6500)).save(buffer, format='PNG')
        data = buffer.getvalue()
        backend.files.append(SimpleNamespace(name='unsupported.png', type='image/png', getvalue=lambda: data))
    app.run(timeout=20)
    assert not app.exception and app.error
    assert button(app, 'Save selected maps').disabled
    assert button(app, 'Extract and review evidence').disabled
    assert not backend.upload_calls and len(backend.current()['maps']) == 1
    assert not backend.jobs


def test_limits_include_persisted_maps_and_exclude_reselected_saved_images():
    backend = UploadBackend()
    first, second = image_file('first.png'), image_file('second.png', 'red')
    run = backend.add_saved(first)
    limits = backend.service.info()['limits']
    pending = pending_maps([first, second], run['maps'], limits)
    assert [m['file'].name for m in pending] == ['second.png']
    with pytest.raises(ValueError, match='combined'):
        pending_maps([second], run['maps'], {**limits, 'total_bytes': first.size + second.size - 1})
    oversized = SimpleNamespace(name='large.png', getvalue=lambda: b'x' * 30_000_001)
    with pytest.raises(ValueError, match='30 MB'):
        pending_maps([oversized], [], limits)


def test_reopening_saved_package_does_not_upload_identical_maps_again(ui):
    app, backend = ui
    backend.files = [image_file('saved.png')]
    backend.add_saved(backend.files[0])
    app.run(timeout=20)
    assert not app.exception and not app.error
    assert button(app, 'Save selected maps').disabled
    assert not button(app, 'Extract and review evidence').disabled
    assert not backend.upload_calls
