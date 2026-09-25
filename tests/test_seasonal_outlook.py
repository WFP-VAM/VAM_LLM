"""Offline scientific contract, phase graph, analysis record and transport regression tests."""
import copy
import io
import json
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import pytest
from PIL import Image
from app.services.seasonal_outlook.config import Settings
from app.services.seasonal_outlook.storage import MemoryStore, Conflict
from app.services.seasonal_outlook.service import DEADLINE_MARGIN_SECONDS, Gone, Service, Unavailable, ordered
from app.services.seasonal_outlook.runner import run_phase
from app.services.seasonal_outlook import engine
from app.services.seasonal_outlook.inputs import regions, inspect_image, season_mode
from app.services.seasonal_outlook.provider import vertex_schema


class Launcher:
    """Records the phases the service starts; tests run them with perform()."""
    def __init__(self):
        self.launched = []

    def __call__(self, run, op):
        self.launched.append((run, op))


class FakeProvider:
    """Synthetic readings exercise contracts, never act as scientific reference truth."""
    def __init__(self, fail=None, finish='STOP'):
        self.requests = []
        self.fail = fail
        self.finish = finish

    def complete(self, request, timeout):
        self.requests.append(copy.deepcopy(request))
        p, stage = request['payload'], request['stage']
        if stage == self.fail:
            raise TimeoutError('Synthetic transport failure; outcome uncertain')
        if stage == 'extraction':
            kind = 'forecast' if all(s['mode'] in ('future_only', 'excluded') for s in p['case']['calendar']) else 'observed'
            value = dict(maps=[dict(figure_id=fid, title_as_read='Synthetic rainfall map', product_kind=kind,
                variable='Rainfall', metric='percent of average', units='%', reference_baseline='1991–2020',
                period_as_read='August 2026', valid_start='2026-08-01', valid_end='2026-08-31', issue_date='2026-09-01',
                geographic_coverage='Northern test area', legend_as_read='40–60%', readability='readable', limitations=[],
                signals=[dict(geography='Northern test area', signal='Below-average rainfall', magnitude_as_read='40–60%',
                    legend_class='40–60%', location_in_figure='Upper left', confidence='medium', limitations=[])])
                for fid in p['case']['figure_ids']])
        elif stage == 'review':
            value = dict(overall_limitations=[], maps=[dict(figure_id=m['figure_id'], coverage_summary='Inspected synthetic map.',
                issues=[], limitations=[]) for m in p['initial_extraction']['maps']])
        elif stage == 'refinement':
            value = dict(evidence=p['initial_extraction'], issue_resolutions=[], limitations=[])
        elif stage == 'feedback':
            evidence = p['latest_extraction']
            signal = evidence['maps'][0]['signals'][0]
            signal['limitations'].append('An exact value cannot be read.')
            value = dict(evidence=evidence, resolutions=[dict(analyst_quote=p['analyst_comments'], decision='unverifiable',
                explanation='The map supports a range only.', evidence_ids=[signal['source_id']])], limitations=[])
        elif stage == 'report_review':
            value = dict(issues=[], limitations=['No independent visual verification.'])
        else:
            if stage == 'redraft':
                report = copy.deepcopy(p['initial_analysis'])
                report.pop('headline_id')
                for paragraph in report['paragraphs']:
                    paragraph.pop('block_id')
            else:
                evidence = p['evidence']['maps'][0]['signals'][0]['evidence_id']
                season = next(s['season_id'] for s in p['case']['calendar'] if s['mode'] in ('past_and_future', 'past_only'))
                report = dict(headline='Below-average rainfall in the north', headline_evidence_ids=[evidence],
                    paragraphs=[dict(text='The northern test area recorded 40–60% of average rainfall.',
                                     evidence_ids=[evidence], season_ids=[season])], limitations=[])
            value = dict(report=report, inability_reason=None)
        return dict(text=json.dumps(value), finish_reason=self.finish,
                    diagnostic=dict(model='synthetic-test', token_usage=dict(prompt_token_count=10, candidates_token_count=20)))


@pytest.fixture
def service():
    settings = Settings(enabled=True, project='company-test', bucket='company-test', signer='worker@company-test.iam.gserviceaccount.com')
    return Service(settings, MemoryStore(), Launcher())


def image_bytes(fmt='PNG'):
    buf = io.BytesIO()
    Image.new('RGB', (100, 80), 'white').save(buf, format=fmt)
    return buf.getvalue()


def prepared(service, region='eastern_africa_yemen'):
    run = service.create(dict(request_id=uuid.uuid4().hex, expected_revision=0,
        region_id=region, report_date='2026-09-16', notes='Synthetic offline test'))
    return service.upload(run['id'], dict(request_id=uuid.uuid4().hex, expected_revision=run['revision'],
        product='observed', note='', issue_date='2026-09-01'), image_bytes(), 'map.png')


def action(service, run, kind, **fields):
    return service.action(run['id'], kind, dict(request_id=uuid.uuid4().hex, expected_revision=run['revision'], **fields))


def perform(service, run, provider=None):
    run_phase(service, run['id'], run['active'], provider or FakeProvider())
    return service.get(run['id'])


@pytest.mark.parametrize('code', ['AFY', 'AMX', 'ASE'])
def test_complete_workflow_and_exports(service, code):
    region = 'eastern_africa_yemen' if code == 'AFY' else next(r['region_id'] for r in regions() if code in r['map_codes'])
    fake = FakeProvider()
    run = perform(service, action(service, prepared(service, region), 'extract'), fake)
    assert run['status'] == 'awaiting_review' and len(run['versions']) == 2
    with pytest.raises(Conflict):
        action(service, run, 'confirm', version_id=run['current_evidence'], confirmed=False)
    run = perform(service, action(service, run, 'feedback', version_id=run['current_evidence'], comments='Can we read an exact number?'), fake)
    assert len(run['versions']) == 3 and run['confirmation'] is None
    run = perform(service, action(service, run, 'confirm', version_id=run['current_evidence'], confirmed=True), fake)
    assert run['status'] == 'completed' and len(run['artifacts']) == 3
    for request in fake.requests:
        assert '$defs' not in json.dumps(vertex_schema(request['schema']))
        if request['stage'] in ('draft', 'report_review', 'redraft'):
            assert not request['images']
            assert 'analyst_comments' not in request['payload']
            assert set(request['payload']) <= {'case', 'evidence', 'initial_analysis', 'draft_review'}
    from docx import Document
    for name in ('report.docx', 'report-with-maps.docx'):
        doc = Document(io.BytesIO(service.store.read(run['artifacts'][name])))
        assert bool(doc.inline_shapes) == ('with-maps' in name)
        assert any('40–60%' in p.text for p in doc.paragraphs)
    with zipfile.ZipFile(io.BytesIO(service.store.read(run['artifacts']['artifacts.zip']))) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
        assert {'input.json', 'evidence.json', 'report.json', 'report.docx', 'maps/01.png'} <= names
        # Earlier operations contribute their outputs; nothing records intermediate checkpoints.
        earlier = ordered(run['operations'])[:-1]
        assert {f'operations/{o["id"]}/output.json' for o in earlier} <= names
        assert not any('checkpoint' in name for name in names)
    assert [r['stage'] for r in fake.requests] == ['extraction', 'review', 'refinement', 'feedback',
                                                   'draft', 'report_review', 'redraft']


def test_phase_graph_has_no_checkpointer():
    from app.services.seasonal_outlook.graph import build_graph
    graph = build_graph(FakeProvider(), None, timeout=600, namespace='test', export=lambda state: {})
    assert graph.checkpointer is None
    assert set(graph.nodes) >= {'extraction', 'review', 'refinement', 'feedback', 'draft', 'report_review', 'redraft', 'export'}


def test_idempotence_conflicts_and_single_start(service):
    run = prepared(service)
    body = dict(request_id=uuid.uuid4().hex, expected_revision=run['revision'])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.action(run['id'], 'extract', body), range(2)))
    assert len(service.launch.launched) == 1
    assert results[0]['active'] == results[1]['active']
    with pytest.raises(Conflict):
        service.action(run['id'], 'extract', {**body, 'timeout': 1200})
    with pytest.raises(Conflict):
        action(service, run, 'extract')
    fake = FakeProvider()
    run_phase(service, run['id'], results[0]['active'], fake)
    with pytest.raises(Conflict):  # A second start of the same operation stops before any inference.
        run_phase(service, run['id'], results[0]['active'], fake)
    assert len(fake.requests) == 3


def test_retry_runs_the_whole_failed_phase_from_its_inputs(service):
    run = action(service, prepared(service), 'extract')
    failed = run['active']
    with pytest.raises(TimeoutError):
        perform(service, run, FakeProvider(fail='review'))
    run = service.get(run['id'])
    operation = run['operations'][failed]
    assert run['status'] == 'failed' and operation['completed'] == ['extraction']
    assert len(operation['responses']) == 1 and not operation.get('output')
    # A failed phase publishes only its diagnostics, never a partial evidence version.
    assert not run['versions'] and run['current_evidence'] is None
    with pytest.raises(ValueError):
        action(service, run, 'retry', operation_id=failed, timeout=900)
    retried = action(service, run, 'retry', operation_id=failed, timeout=1800)
    successor = retried['operations'][retried['active']]
    assert successor['input'] == operation['input'] and successor['retry_of'] == failed
    fake = FakeProvider()
    run = perform(service, retried, fake)
    assert [r['stage'] for r in fake.requests] == ['extraction', 'review', 'refinement']
    assert run['status'] == 'awaiting_review' and len(run['versions']) == 2


def test_deadline_expiry_interrupts_and_a_late_thread_cannot_overwrite(service):
    now = [1000.0]
    service.clock = lambda: now[0]
    run = action(service, prepared(service), 'extract')
    late = run['active']
    assert run['operations'][late]['deadline'] == 1000.0 + 3 * 600 + DEADLINE_MARGIN_SECONDS

    class Slow(FakeProvider):
        def complete(self, request, timeout):
            now[0] += 3 * 600 + DEADLINE_MARGIN_SECONDS + 1  # This call outlives the phase deadline.
            interrupted = service.get(run['id'])
            assert interrupted['status'] == 'interrupted' and interrupted['active'] is None
            action(service, interrupted, 'retry', operation_id=late, timeout=1200)
            return super().complete(request, timeout)
    with pytest.raises(Conflict):
        perform(service, run, Slow())
    current = service.get(run['id'])
    successor = current['active']
    assert successor != late and current['operations'][successor]['status'] == 'queued'
    # The late thread wrote neither success nor failure, and no response or evidence.
    assert current['operations'][late]['status'] == 'interrupted'
    assert not current['operations'][late]['responses'] and not current['versions']
    assert perform(service, current)['status'] == 'awaiting_review'


@pytest.mark.parametrize('finish', ['MAX_TOKENS', 'SAFETY', 'BLOCKED'])
def test_invalid_response_saved_before_failure(service, finish):
    run = action(service, prepared(service), 'extract')
    op = run['active']
    with pytest.raises(ValueError):
        perform(service, run, FakeProvider(finish=finish))
    run = service.get(run['id'])
    failed = run['operations'][op]
    assert len(failed['responses']) == 1 and not failed.get('output') and not failed['completed']
    assert failed['calls'][0]['status'] == 'failed'
    assert not run['versions']


def test_confirmation_invalidated_and_old_report_cannot_be_retried(service):
    run = perform(service, action(service, prepared(service), 'extract'))
    evidence = run['current_evidence']
    run = perform(service, action(service, run, 'confirm', version_id=evidence, confirmed=True))
    report_op = ordered(run['operations'])[-1]['id']
    run = perform(service, action(service, run, 'feedback', version_id=evidence, comments='Verify the number.'))
    assert run['confirmation'] is None and not run['artifacts']
    with pytest.raises(Conflict):
        action(service, run, 'retry', operation_id=report_op)
    with pytest.raises(Conflict):
        action(service, run, 'confirm', version_id=evidence, confirmed=True)


def test_report_retry_requires_the_confirmed_evidence_hash(service):
    run = perform(service, action(service, prepared(service), 'extract'))
    run = action(service, run, 'confirm', version_id=run['current_evidence'], confirmed=True)
    report = run['active']
    with pytest.raises(TimeoutError):
        perform(service, run, FakeProvider(fail='redraft'))
    run = service.get(run['id'])
    assert run['status'] == 'failed' and not run['artifacts']
    confirmation = run['confirmation']

    def confirm_as(value):
        def change(current):
            current['confirmation'] = value
            return current
        return change
    run = service.store.mutate(run['id'], confirm_as({**confirmation, 'evidence_sha256': '0' * 64}))
    with pytest.raises(Conflict):
        action(service, run, 'retry', operation_id=report)
    run = service.store.mutate(run['id'], confirm_as(confirmation))
    fake = FakeProvider()
    run = perform(service, action(service, run, 'retry', operation_id=report), fake)
    assert [r['stage'] for r in fake.requests] == ['draft', 'report_review', 'redraft']
    assert run['status'] == 'completed' and len(run['artifacts']) == 3


def test_default_launcher_runs_the_phase_in_a_background_thread(service, monkeypatch):
    import time
    from app.services.seasonal_outlook import runner
    monkeypatch.setattr(runner, 'VertexProvider', lambda settings: FakeProvider())
    threaded = Service(service.settings, service.store)
    run = action(threaded, prepared(threaded), 'extract')
    finish_by = time.monotonic() + 60
    while threaded.get(run['id'])['status'] in ('queued', 'running') and time.monotonic() < finish_by:
        time.sleep(0.05)
    run = threaded.get(run['id'])
    assert run['status'] == 'awaiting_review' and len(run['versions']) == 2


def test_a_phase_that_cannot_start_fails_and_can_be_retried(service):
    def broken(run_id, operation_id):
        raise RuntimeError("can't start new thread")
    service.launch = broken
    run = action(service, prepared(service), 'extract')
    operation = ordered(run['operations'])[-1]
    assert run['status'] == 'failed' and run['active'] is None
    assert operation['status'] == 'failed' and 'could not start' in operation['error']
    service.launch = Launcher()
    retried = action(service, run, 'retry', operation_id=operation['id'])
    assert retried['status'] == 'queued' and service.launch.launched == [(run['id'], retried['active'])]


def test_analyses_of_the_previous_workflow_are_listed_but_gone(service, monkeypatch):
    from app.services.seasonal_outlook import api
    run = prepared(service)

    def previous_workflow(current):
        current.pop('workflow_revision')
        return current
    service.store.mutate(run['id'], previous_workflow)
    assert [r['id'] for r in service.list()] == [run['id']]
    with pytest.raises(Gone):
        service.get(run['id'])
    monkeypatch.setattr(api, 'get_service', lambda: service)
    reply = api.handle('GET', ['runs', run['id']])
    assert reply.status == 410 and 'previous version' in reply.data['detail']


def test_feature_flag_keeps_history_readable(service):
    run = prepared(service)
    disabled = Service(replace(service.settings, enabled=False), service.store, service.launch)
    assert disabled.get(run['id'])['id'] == run['id']
    with pytest.raises(Unavailable):
        action(disabled, run, 'extract')
    assert Settings(backend='memory').errors()


def test_enabled_default_still_requires_company_configuration(monkeypatch):
    import os
    for name in list(os.environ):
        if name.startswith('SEASONAL_'):
            monkeypatch.delenv(name)
    settings = Settings.from_env()
    assert settings.enabled is True
    service = Service(settings, MemoryStore(), Launcher())
    assert service.info()['enabled'] is False
    assert service.info()['configuration_errors']
    with pytest.raises(Unavailable):
        prepared(service)
    assert not service.launch.launched
    for key, value in dict(project='company-test', bucket='company-test',
                           signer='worker@company-test.iam.gserviceaccount.com').items():
        monkeypatch.setenv('SEASONAL_' + key.upper(), value)
    configured = Settings.from_env()
    assert Service(configured, MemoryStore(), Launcher()).info()['enabled'] is True
    monkeypatch.setenv('SEASONAL_DRAFTER_ENABLED', 'false')
    assert Settings.from_env().enabled is False
    monkeypatch.setenv('SEASONAL_DRAFTER_ENABLED', ' TRUE ')
    assert Settings.from_env().enabled is True


def test_input_limits_dates_and_immutable_inputs(service):
    with pytest.raises(ValueError):
        inspect_image(b'x'*30_000_001, 'large.png')
    with pytest.raises(ValueError):
        inspect_image(b'invalid', 'map.png')
    assert inspect_image(image_bytes('WEBP'), 'map.webp') == '.webp'
    run = prepared(service)
    with pytest.raises(ValueError):
        service.upload(run['id'], dict(request_id=uuid.uuid4().hex, expected_revision=run['revision'], product='observed'), image_bytes(), 'duplicate.png')
    assert season_mode(12, [1, 2, 3]) == 'future_only'
    assert season_mode(4, [1, 2, 3]) == 'past_only'
    run = action(service, run, 'extract')
    with pytest.raises(Conflict):
        service.upload(run['id'], dict(request_id=uuid.uuid4().hex, expected_revision=run['revision'], product='observed'), image_bytes('WEBP'), 'new.webp')


def test_unknown_ids_and_empty_json_rejected(service):
    run = prepared(service)
    state = engine.initial_state(service.validate(run['id']), run['maps'])
    request = engine.request_for('extraction', state)
    response = FakeProvider().complete(request, 600)
    value = json.loads(response['text'])
    value['maps'][0]['figure_id'] = 'invented'
    with pytest.raises(ValueError):
        engine.accept('extraction', state, {**response, 'text': json.dumps(value)}, 'test')
    for text in ('', '{invalid', '{}'):
        with pytest.raises(ValueError):
            engine.accept('extraction', state, {**response, 'text': text}, 'test')


def test_fastapi_and_local_dispatch_share_service(service, monkeypatch):
    from app.services.seasonal_outlook import api
    monkeypatch.setattr(api, 'get_service', lambda: service)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.services.seasonal_outlook.router import router
    app = FastAPI()
    app.include_router(router, prefix='/seasonal-outlook')
    client = TestClient(app)
    body = dict(request_id=uuid.uuid4().hex, expected_revision=0, region_id='eastern_africa_yemen', report_date='2026-09-16')
    created = client.post('/seasonal-outlook/runs', json=body)
    assert created.status_code == 200
    rid = created.json()['id']
    response = client.post(f'/seasonal-outlook/runs/{rid}/maps', data=dict(request_id=uuid.uuid4().hex, expected_revision=1, product='observed'), files={'file': ('map.png', image_bytes(), 'image/png')})
    assert response.status_code == 200, response.text
    from app.streamlit_backend.dispatcher import dispatch_request
    local = dispatch_request('GET', f'/seasonal-outlook/runs/{rid}')
    assert local.status_code == 200 and local.json() == response.json()
    assert client.get('/seasonal-outlook/runs').json()[0]['id'] == rid


def test_gemini_configuration_and_gcs_images(service, monkeypatch):
    from google import genai
    from google.genai import types
    from app.services.seasonal_outlook.provider import VertexProvider
    captured = {}
    class Client:
        def __init__(self, **kwargs):
            captured['client'] = kwargs
            self.models = self
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def generate_content(self, **kwargs):
            captured['request'] = kwargs
            return types.GenerateContentResponse(candidates=[types.Candidate(finish_reason='STOP',
                content=types.Content(parts=[types.Part(text='{}')]))], model_version='gemini-3.1-pro-preview')
    monkeypatch.setattr(genai, 'Client', Client)
    run = prepared(service)
    state = engine.initial_state(service.validate(run['id']), run['maps'])
    request = engine.request_for('extraction', state)
    response = VertexProvider(service.settings).complete(request, 1200)
    config = captured['request']['config']
    assert config.temperature == 1.0 and config.max_output_tokens == 32768
    assert config.thinking_config.thinking_level == 'HIGH'
    assert config.media_resolution == 'MEDIA_RESOLUTION_HIGH'
    assert captured['client']['vertexai'] is True
    assert captured['client']['http_options'].timeout == 1_200_000
    assert captured['client']['http_options'].retry_options.attempts == 1
    assert captured['client']['project'] == 'company-test'
    assert captured['request']['contents'].parts[-1].file_data.file_uri.startswith('gs://')
    assert response['finish_reason'] == 'STOP' and response['text'] == '{}'


def test_stale_feedback_loses_to_other_analyst(service):
    run = perform(service, action(service, prepared(service), 'extract'))
    original = copy.deepcopy(run)
    run = perform(service, action(service, run, 'feedback', version_id=run['current_evidence'], comments='Check uncertainty.'))
    with pytest.raises(Conflict):
        action(service, original, 'confirm', version_id=original['current_evidence'], confirmed=True)
    assert service.get(run['id'])['current_evidence'] == run['current_evidence']


def open_page(service, monkeypatch, run_id):
    """Render page 5 against the service in process; returns the app and the requests it made."""
    import sys
    from types import ModuleType
    from pathlib import Path
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    from app.services.seasonal_outlook import api
    monkeypatch.setattr(api, 'get_service', lambda: service)
    monkeypatch.setattr(api, 'service_info', service.info)
    requests = []
    shared = ModuleType('streamlit_shared')
    for name in ('apply_wfp_theme', 'render_wfp_sidebar_logo', 'render_onboarding_sidebar_button',
                 'render_instructions_sidebar_button', 'render_bug_report_sidebar_link', 'render_bug_report_header_link'):
        setattr(shared, name, lambda *a, **k: None)
    def request(method, path, **kwargs):
        requests.append((method, path))
        reply = api.handle(method, path.split('/')[2:], kwargs.get('json_body'), kwargs.get('params'))
        if reply.status >= 400:
            raise RuntimeError(reply.data)
        return reply.data if reply.data is not None else reply.content
    shared.request_json = request
    shared.request_bytes = request
    shared.safe_show_error = lambda exc: st.error(str(exc))
    monkeypatch.setitem(sys.modules, 'streamlit_shared', shared)
    page = Path(__file__).parents[1] / 'pages/5_Seasonal_Outlook_Drafter.py'
    app = AppTest.from_file(str(page))
    app.query_params['seasonal_run'] = run_id
    app.run(timeout=20)
    return app, requests


def test_ui_refresh_and_tabs_never_start_inference(service, monkeypatch):
    run = perform(service, action(service, prepared(service), 'extract'))
    app, requests = open_page(service, monkeypatch, run['id'])
    assert not app.exception
    assert [t.label for t in app.tabs] == ['Input package', 'Evidence and analyst review', 'Report and downloads']
    assert next(x for x in app.selectbox if x.label == 'Region').value is None
    confirm = next(b for b in app.button if b.label == 'Confirm evidence and draft report')
    assert confirm.disabled
    next(b for b in app.button if b.label == 'Refresh analysis').click().run(timeout=20)
    assert not app.exception
    assert all(method == 'GET' for method, _ in requests)
    next(b for b in app.button if b.label == 'Start another input package').click().run(timeout=20)
    assert not app.exception
    assert not app.query_params.get('seasonal_run')
    assert not next(b for b in app.button if b.label == 'Prepare input package').disabled
    assert all(method == 'GET' for method, _ in requests)


def test_ui_offers_retry_only_for_the_latest_failed_operation(service, monkeypatch):
    run = action(service, prepared(service), 'extract')
    with pytest.raises(TimeoutError):
        perform(service, run, FakeProvider(fail='review'))
    app, requests = open_page(service, monkeypatch, run['id'])
    assert not app.exception
    assert any('Synthetic transport failure' in error.value for error in app.error)
    retry = next(b for b in app.button if b.label == 'Retry failed operation')
    assert not retry.disabled
    retry.click().run(timeout=20)
    assert not app.exception
    assert ('POST', f'/seasonal-outlook/runs/{run["id"]}/retry') in requests
    current = service.get(run['id'])
    assert current['status'] == 'queued' and service.launch.launched[-1] == (run['id'], current['active'])
    # st.rerun() leaves the aborted run's widgets in AppTest's tree, so check a fresh session.
    app, _ = open_page(service, monkeypatch, run['id'])
    assert not app.exception
    assert not any(b.label == 'Retry failed operation' for b in app.button)


def test_real_sdk_wire_conversion_without_network(service, monkeypatch):
    """Exercise the actual SDK's schema/URI serialization with a local HTTP transport."""
    import httpx
    from google import genai
    from google.oauth2.credentials import Credentials
    from app.services.seasonal_outlook.provider import VertexProvider
    real_client = genai.Client
    calls, wire_response = [], {}
    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert str(request.url).startswith('https://aiplatform.googleapis.com/')
        assert '/projects/company-test/locations/global/' in str(request.url)
        assert request.headers['X-Vertex-AI-LLM-Request-Type'] == 'shared'
        config = payload['generationConfig']
        thinking = config['thinkingConfig']
        assert thinking.get('thinkingLevel', thinking.get('thinking_level')) == 'HIGH'
        assert '$ref' not in json.dumps(config['responseSchema'])
        assert config['responseMimeType'] == 'application/json'
        return httpx.Response(200, json=wire_response)
    def factory(**kwargs):
        options = kwargs.pop('http_options')
        options.httpx_client = httpx.Client(transport=httpx.MockTransport(transport))
        return real_client(credentials=Credentials(token='synthetic-offline-test-token'), http_options=options, **kwargs)
    monkeypatch.setattr(genai, 'Client', factory)
    run = prepared(service)
    state = engine.initial_state(service.validate(run['id']), run['maps'])
    provider = VertexProvider(service.settings)
    fake = FakeProvider()
    for stage in ('extraction', 'review', 'refinement', 'draft', 'report_review', 'redraft'):
        request = engine.request_for(stage, state)
        response = fake.complete(request, 600)
        wire_response = dict(candidates=[dict(content=dict(role='model', parts=[dict(text=response['text'])]), finishReason='STOP')],
                             modelVersion='gemini-3.1-pro-preview', usageMetadata=dict(promptTokenCount=10, candidatesTokenCount=20))
        result = provider.complete(request, 600)
        state = engine.accept(stage, state, result, 'wire-test')
    assert len(calls) == 6
    assert calls[0]['generationConfig']['maxOutputTokens'] == 32768
    assert calls[-1]['generationConfig']['maxOutputTokens'] == 65536
    assert 'fileData' in calls[0]['contents'][0]['parts'][-1]
    assert len(calls[-1]['contents'][0]['parts']) == 1


def test_sdk_does_not_retry_server_error(service, monkeypatch):
    import httpx
    from google import genai
    from google.oauth2.credentials import Credentials
    from app.services.seasonal_outlook.provider import VertexProvider
    real_client, calls = genai.Client, []
    def transport(request):
        calls.append(request)
        return httpx.Response(503, json={'error': {'code': 503, 'message': 'Synthetic unavailability', 'status': 'UNAVAILABLE'}})
    def factory(**kwargs):
        options = kwargs.pop('http_options')
        options.httpx_client = httpx.Client(transport=httpx.MockTransport(transport))
        return real_client(credentials=Credentials(token='synthetic-offline-test-token'), http_options=options, **kwargs)
    monkeypatch.setattr(genai, 'Client', factory)
    run = prepared(service)
    state = engine.initial_state(service.validate(run['id']), run['maps'])
    with pytest.raises(genai.errors.ServerError):
        VertexProvider(service.settings).complete(engine.request_for('extraction', state), 600)
    assert len(calls) == 1


def test_large_word_appendix_is_bounded_but_original_is_preserved(service):
    buf = io.BytesIO()
    Image.new('RGB', (2400, 2000), 'navy').save(buf, format='WEBP')
    original = buf.getvalue()
    run = service.create(dict(request_id=uuid.uuid4().hex, expected_revision=0,
        region_id='eastern_africa_yemen', report_date='2026-09-16'))
    run = service.upload(run['id'], dict(request_id=uuid.uuid4().hex, expected_revision=1,
        product='observed'), original, 'large.webp')
    run = perform(service, action(service, run, 'extract'))
    run = perform(service, action(service, run, 'confirm', version_id=run['current_evidence'], confirmed=True))
    with zipfile.ZipFile(io.BytesIO(service.store.read(run['artifacts']['report-with-maps.docx']))) as doc:
        picture = next(n for n in doc.namelist() if n.startswith('word/media/'))
        with Image.open(io.BytesIO(doc.read(picture))) as rendered:
            assert rendered.width <= 1860 and rendered.height <= 1800
    with zipfile.ZipFile(io.BytesIO(service.store.read(run['artifacts']['artifacts.zip']))) as package:
        assert package.read('maps/01.webp') == original
