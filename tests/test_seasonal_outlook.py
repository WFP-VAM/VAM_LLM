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
from app.shared.runs.store import MemoryStore, Conflict
from app.services.seasonal_outlook.service import DEADLINE_MARGIN_SECONDS, Gone, Service, Unavailable, ordered
from app.services.seasonal_outlook.runner import run_phase
from app.services.seasonal_outlook import engine, nodes
from app.services.seasonal_outlook.inputs import regions, inspect_image, season_mode
from app.services.seasonal_outlook.calls import llm_request
from app.services.seasonal_outlook import runner
from app.shared.llm import FilePart, LLMCallError, LLMClient, LLMResponse, create_llm_client


class Launcher:
    """Records the phases the service starts; tests run them with perform()."""
    def __init__(self):
        self.launched = []

    def __call__(self, run, op):
        self.launched.append((run, op))


class FakeProvider:
    """Synthetic readings exercise contracts, never act as scientific reference truth.

    The model as the shared LLM client sees it. Each request is recorded with its stage, payload, images and
    response schema. `fail` names a stage that always fails in transport; `failures` counts how often it does.
    """
    def __init__(self, fail=None, outcome='completed', failures=None):
        self.requests = []
        self.fail = fail
        self.outcome = outcome
        self.failures = failures

    def generate(self, _profile, llm_request):
        p, stage = json.loads(llm_request.parts[0]), llm_request.node
        self.requests.append(dict(stage=stage, payload=copy.deepcopy(p), schema=llm_request.response_schema,
                                  images=[part for part in llm_request.parts if isinstance(part, FilePart)]))
        if stage == self.fail and (self.failures is None or self.failures > 0):
            self.failures = None if self.failures is None else self.failures - 1
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
        return LLMResponse(text=json.dumps(value), outcome=self.outcome, finish_reason='synthetic-completion', model_version='synthetic-test',
                           usage=dict(prompt_tokens=10, candidate_tokens=20, thought_tokens=None, total_tokens=None),
                           raw=dict(opaque=['uninterpreted', 'diagnostic']))


def reply(provider, request):
    """The fake's reply to an app-level stage request, as nodes.accept reads it."""
    response = provider.generate(None, llm_request(request, 600, 'test'))
    return dict(text=response.text, outcome=response.outcome)


@pytest.fixture(autouse=True)
def immediate_retries(monkeypatch):
    """The client pauses before retrying a transient failure; the tests need not wait."""
    monkeypatch.setattr(runner, 'create_llm_client', lambda *args, **kwargs: create_llm_client(*args, **kwargs, sleep=lambda _: None))


@pytest.fixture
def service():
    settings = Settings(enabled=True, project='company-test', bucket='company-test', signer='worker@company-test.iam.gserviceaccount.com')
    return Service(settings, MemoryStore('seasonal-outlook', missing='Analysis not found'), Launcher())


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
        assert request['schema']['type'] == 'object'  # Standard application JSON schema.
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
    graph = build_graph(None, None, timeout=600, namespace='test', export=lambda state: {})
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
    with pytest.raises(LLMCallError) as caught:
        perform(service, run, FakeProvider(fail='review'))
    assert isinstance(caught.value.__cause__, TimeoutError)
    run = service.get(run['id'])
    operation = run['operations'][failed]
    assert run['status'] == 'failed' and operation['completed'] == ['extraction']
    assert len(operation['responses']) == 1 and not operation.get('output')
    # The transient failure was retried once before the phase failed; both attempts are on record.
    assert [(c['stage'], c['attempt'], c['status']) for c in operation['calls']] == [
        ('extraction', 1, 'validated'), ('review', 1, 'failed'), ('review', 2, 'failed')]
    assert operation['calls'][-1]['failure_code'] == 'llm_transport_error'
    assert operation['error'] == 'TimeoutError: Synthetic transport failure; outcome uncertain'
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


def test_a_single_transient_failure_is_retried_within_the_phase(service):
    run = action(service, prepared(service), 'extract')
    op = run['active']
    fake = FakeProvider(fail='review', failures=1)
    run = perform(service, run, fake)
    assert run['status'] == 'awaiting_review' and len(run['versions']) == 2
    assert [r['stage'] for r in fake.requests] == ['extraction', 'review', 'review', 'refinement']
    calls = run['operations'][op]['calls']
    assert [(c['stage'], c['attempt'], c['status']) for c in calls] == [
        ('extraction', 1, 'validated'), ('review', 1, 'failed'), ('review', 2, 'validated'), ('refinement', 1, 'validated')]
    assert run['operations'][op]['completed'] == ['extraction', 'review', 'refinement']


def test_deadline_expiry_interrupts_and_a_late_thread_cannot_overwrite(service):
    now = [1000.0]
    service.clock = lambda: now[0]
    run = action(service, prepared(service), 'extract')
    late = run['active']
    assert run['operations'][late]['deadline'] == 1000.0 + 3 * 600 + DEADLINE_MARGIN_SECONDS

    class Slow(FakeProvider):
        def generate(self, profile, request):
            now[0] += 3 * 600 + DEADLINE_MARGIN_SECONDS + 1  # This call outlives the phase deadline.
            interrupted = service.get(run['id'])
            assert interrupted['status'] == 'interrupted' and interrupted['active'] is None
            action(service, interrupted, 'retry', operation_id=late, timeout=1200)
            return super().generate(profile, request)
    with pytest.raises(Conflict):
        perform(service, run, Slow())
    current = service.get(run['id'])
    successor = current['active']
    assert successor != late and current['operations'][successor]['status'] == 'queued'
    # The late thread wrote neither success nor failure, and no response or evidence.
    assert current['operations'][late]['status'] == 'interrupted'
    assert not current['operations'][late]['responses'] and not current['versions']
    assert perform(service, current)['status'] == 'awaiting_review'


@pytest.mark.parametrize('outcome,code,error', [
    ('truncated', 'llm_response_truncated', 'ValueError: LLM response stopped at synthetic-completion'),
    ('blocked', 'llm_response_blocked', 'ValueError: Model response is blocked'),
    ('unknown', 'llm_response_unknown', 'ValueError: Model response is unknown')])
def test_invalid_response_saved_before_failure(service, outcome, code, error):
    run = action(service, prepared(service), 'extract')
    op = run['active']
    with pytest.raises(LLMCallError) as caught:
        perform(service, run, FakeProvider(outcome=outcome))
    assert caught.value.failure_code == code
    run = service.get(run['id'])
    failed = run['operations'][op]
    assert len(failed['responses']) == 1 and not failed.get('output') and not failed['completed']
    assert (failed['calls'][0]['status'], failed['calls'][0]['failure_code']) == ('failed', code)
    assert failed['error'] == failed['calls'][0]['error'] == error
    assert not run['versions']


def test_phase_errors_are_stored_without_credentials(service):
    class Leaky(FakeProvider):
        def generate(self, profile, request):
            raise RuntimeError('upstream rejected api_key=AIzaSECRET, token: abc123')
    run = action(service, prepared(service), 'extract')
    op = run['active']
    with pytest.raises(RuntimeError):
        perform(service, run, Leaky())
    failed = service.get(run['id'])['operations'][op]
    for error in (failed['error'], failed['calls'][-1]['error']):
        assert 'AIzaSECRET' not in error and 'abc123' not in error
        assert 'api_key=[redacted]' in error


def test_api_replies_do_not_expose_storage_uris(service, monkeypatch):
    from app.services.seasonal_outlook import api
    monkeypatch.setattr(api, 'get_service', lambda: service)
    run = perform(service, action(service, prepared(service), 'extract'))
    op = next(iter(run['operations']))
    assert 'gs://' not in json.dumps(run)  # Only shared adapters resolve original object references.
    for parts in (['runs', run['id']], ['runs', run['id'], 'operations'], ['runs', run['id'], 'operations', op],
                  ['runs', run['id'], 'versions']):
        reply = api.handle('GET', parts)
        assert reply.status == 200 and 'gs://' not in json.dumps(reply.data), parts


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
    with pytest.raises(LLMCallError):
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
    monkeypatch.setattr(runner, 'llm_provider', FakeProvider)
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
    service = Service(settings, MemoryStore('seasonal-outlook', missing='Analysis not found'), Launcher())
    assert service.info()['enabled'] is False
    assert service.info()['configuration_errors']
    with pytest.raises(Unavailable):
        prepared(service)
    assert not service.launch.launched
    for key, value in dict(project='company-test', bucket='company-test',
                           signer='worker@company-test.iam.gserviceaccount.com').items():
        monkeypatch.setenv('SEASONAL_' + key.upper(), value)
    configured = Settings.from_env()
    assert Service(configured, MemoryStore('seasonal-outlook', missing='Analysis not found'), Launcher()).info()['enabled'] is True
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
    request = nodes.request_for('extraction', state)
    response = reply(FakeProvider(), request)
    value = json.loads(response['text'])
    value['maps'][0]['figure_id'] = 'invented'
    with pytest.raises(ValueError):
        nodes.accept('extraction', state, {**response, 'text': json.dumps(value)}, 'test')
    for text in ('', '{invalid', '{}'):
        with pytest.raises(ValueError):
            nodes.accept('extraction', state, {**response, 'text': text}, 'test')


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
    import streamlit_shared
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
    shared.render_llm_diagnostics = streamlit_shared.render_llm_diagnostics
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
    with pytest.raises(LLMCallError):
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


def test_operation_calls_feed_the_shared_diagnostics_panel():
    from datetime import datetime, timezone
    from app.services.seasonal_outlook.ui import llm_diagnostics
    operation = {'id': 'op1', 'calls': [
        dict(stage='extraction', status='validated', call_id='llm-0001', attempt=1, model='gemini-3.1-pro-preview',
             started_at='2026-09-26T10:00:00+00:00', duration_seconds=12.5),
        dict(stage='review', status='failed', call_id='llm-0002', attempt=1, failure_code='llm_invalid_json'),
        dict(stage='review', status='failed', call_id='llm-0003', attempt=2, failure_code='llm_transport_error'),
        dict(stage='extraction', status='validated', attempts=1),  # written before calls had ids
        dict(stage='refinement', status='calling', call_id='llm-0004', started_at=1790000000.0),
    ]}
    diagnostics = llm_diagnostics(operation)
    calls = diagnostics['calls']
    assert [c['status'] for c in calls] == ['succeeded', 'failed', 'failed', 'succeeded', 'started']
    assert [c['operation'] for c in calls][:2] == ['seasonal_outlook.extraction.v1', 'seasonal_outlook.review.v1']
    assert calls[0]['duration_ms'] == 12500 and calls[3]['call_id'] == 'call 4'
    assert calls[4]['started_at'] == datetime.fromtimestamp(1790000000.0, timezone.utc).isoformat()
    assert {k: diagnostics[k] for k in ('current_call_id', 'succeeded_calls', 'recovered_calls', 'failed_calls',
                                        'contract_failed_calls')} == dict(
        current_call_id='llm-0004', succeeded_calls=2, recovered_calls=0, failed_calls=2, contract_failed_calls=1)


def test_a_failed_attempt_made_good_by_the_retry_counts_as_recovered():
    from app.services.seasonal_outlook.ui import llm_diagnostics
    diagnostics = llm_diagnostics({'calls': [
        dict(stage='review', status='failed', call_id='a', failure_code='llm_transport_error'),
        dict(stage='review', status='validated', call_id='b')]})
    assert [(c['status'], c['disposition']) for c in diagnostics['calls']] == [
        ('recovered', 'recovered_by_retry'), ('succeeded', None)]
    assert (diagnostics['succeeded_calls'], diagnostics['recovered_calls'], diagnostics['failed_calls']) == (2, 1, 0)


def test_ui_shows_the_operation_calls_in_the_shared_diagnostics_panel(service, monkeypatch):
    run = action(service, prepared(service), 'extract')
    with pytest.raises(LLMCallError):
        perform(service, run, FakeProvider(fail='review'))
    app, _ = open_page(service, monkeypatch, run['id'])
    assert not app.exception
    assert any('LLM call failed: llm_transport_error; node=review; operation=seasonal_outlook.review.v1'
               in error.value for error in app.error)
    metrics = {metric.label: metric.value for metric in app.metric}
    assert metrics['Failed LLM calls'] == '2' and metrics['Completed LLM calls'] == '1'
