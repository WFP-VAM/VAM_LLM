"""Architectural boundary and backend replacement acceptance tests (no cloud calls)."""
import ast
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.shared.llm import LLMRequest, LLMResponse, LLMCallError, ModelProfile, ProviderError, Tracer, create_llm_client
from app.shared.llm import client as clients
from app.shared.runs.store import MemoryStore, UnsupportedRunVersion
from app.shared.runs import report_runs
from app.services.seasonal_outlook import api as seasonal_api, engine, nodes, runner
from app.services.seasonal_outlook.calls import llm_request
from test_seasonal_outlook import service, prepared, action, FakeProvider, image_bytes


def neutral_profile(service='mfi-drafter', **fields):
    defaults = dict(service=service, model='test-text-engine', provider='lab', location='test-lab',
                    temperature=0.3, timeout_seconds=600, max_output_tokens=10000, attempts=1,
                    max_characters=1_200_000, max_input_tokens=250_000, count_retry_delay_seconds=0)
    return ModelProfile(**{**defaults, **fields})


def request(**fields):
    return LLMRequest(**{**dict(operation='mfi.light.draft_dimensions.v1', node='draft_dimensions', parts=['test'],
                              response_schema={'type': 'object', 'properties': {'a': {'type': 'string'}}}), **fields})


class MeasuredProvider:
    name = 'lab'
    def __init__(self, counts=(42,)):
        self.counts, self.calls, self.counter = list(counts), [], 0
    def count_tokens(self, profile, request):
        self.counter += 1
        result = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        self.calls.append((profile, request))
        if isinstance(result, Exception):
            raise result
        return result
    def generate(self, profile, request):
        return LLMResponse(text='{"a": "ok"}', outcome='completed', finish_reason='native_success', raw=['opaque'])


def client_for(provider, **changes):
    profile = neutral_profile(**changes)
    return create_llm_client('mfi-drafter', profiles={'text': profile, 'summary': replace(profile, timeout_seconds=180)},
                             provider=provider, sleep=lambda _: None)


def test_services_cannot_depend_on_cloud_adapters_or_wire_formats():
    root = Path(__file__).resolve().parents[1] / 'app' / 'services'
    forbidden_modules = ('google', 'boto3', 'botocore', 'vertexai', 'azure', 'app.shared.llm.vertex',
                         'app.shared.llm.vertex_schema', 'app.shared.cloud')
    forbidden_names = {'ModelProfile', 'VertexProvider', 'CloudStore', 'MemoryStore', 'market_monitor_profile', 'mfi_profile'}
    forbidden_literals = ('gs://', 's3://', 'gemini-', 'X-Vertex-AI', 'propertyOrdering', 'usage_metadata',
                          'cloud_sql_postgres', 'cloud-sql-postgres', 'K_REVISION', 'K_SERVICE', 'GOOGLE_', 'VERTEX_')
    failures = []
    for path in root.rglob('*.py'):
        source = path.read_text(encoding='utf-8-sig')
        for node in ast.walk(ast.parse(source)):
            imports = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ''] if isinstance(node, ast.ImportFrom) else []
            if any(module == prefix or module.startswith(prefix + '.') for module in imports for prefix in forbidden_modules):
                failures.append((str(path), node.lineno, 'cloud import'))
            if isinstance(node, ast.ImportFrom) and any(alias.name in forbidden_names for alias in node.names):
                failures.append((str(path), node.lineno, 'concrete adapter/configuration'))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and any(s in node.value for s in forbidden_literals):
                failures.append((str(path), node.lineno, 'cloud literal'))
            if isinstance(node, ast.Attribute) and node.attr == 'raw' and not isinstance(getattr(node, 'ctx', None), ast.Store):
                # Archiving the opaque raw value is allowed; indexing it or reading its attributes is not.
                parents = [p for p in ast.walk(ast.parse(source)) if isinstance(p, (ast.Subscript, ast.Attribute))
                           and getattr(getattr(p, 'value', None), 'lineno', None) == node.lineno
                           and isinstance(getattr(p, 'value', None), ast.Attribute) and p.value.attr == 'raw']
                if parents:
                    failures.append((str(path), node.lineno, 'interpreting raw response'))
    assert failures == []


def test_measurement_is_cached_atomically_and_counts_only_actual_requests():
    provider = MeasuredProvider()
    client = client_for(provider)
    counted = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: client.measure(request(), on_count=lambda: counted.append(1)), range(20)))
    assert provider.counter == len(counted) == 1
    assert len({v.fingerprint for v in values}) == 1 and values[0].input_tokens == 42
    assert provider.calls[0][1].timeout_seconds == 30
    assert client.measure(request(work_item='other')).fingerprint == values[0].fingerprint
    assert client.measure(request(system='different')).fingerprint != values[0].fingerprint
    assert client.measure(request(parts=['different'])).fingerprint != values[0].fingerprint
    different_schema = {'type': 'object', 'properties': {'a': {'type': 'string', 'minLength': 7}}}
    assert client.measure(request(response_schema=different_schema)).fingerprint != values[0].fingerprint
    assert client_for(MeasuredProvider(), model='another-model').measure(request()).fingerprint != values[0].fingerprint
    assert client_for(MeasuredProvider(), temperature=0.9).measure(request()).fingerprint != values[0].fingerprint


def test_budget_refuses_before_counting_if_characters_are_too_many():
    provider = MeasuredProvider()
    result = client_for(provider, max_characters=10).measure(request())
    assert not result.allowed and result.input_tokens is None and provider.counter == 0
    assert not client_for(MeasuredProvider((250001,))).measure(request()).allowed


@pytest.mark.parametrize('value', [None, -1, 1.5, True])
def test_invalid_counts_are_never_silently_estimated(value):
    with pytest.raises(ProviderError, match='invalid token count'):
        client_for(MeasuredProvider((value,))).measure(request())


def test_counting_capability_is_required_and_errors_are_bounded():
    with pytest.raises(ProviderError, match='cannot count'):
        client_for(object()).measure(request())
    provider = MeasuredProvider((ProviderError('busy', transient=True), 8))
    assert client_for(provider).measure(request()).input_tokens == 8 and provider.counter == 2
    provider = MeasuredProvider((ProviderError('denied', kind='authentication'),))
    with pytest.raises(ProviderError):
        client_for(provider).measure(request())
    assert provider.counter == 1


@pytest.mark.parametrize('outcome,code', [('blocked', 'llm_response_blocked'), ('unknown', 'llm_response_unknown'),
                                        ('truncated', 'llm_response_truncated')])
def test_outcomes_are_normalized_even_when_native_finish_says_success(outcome, code):
    class Provider(MeasuredProvider):
        def generate(self, profile, request):
            return LLMResponse(text='{"a": "ok"}', outcome=outcome, finish_reason='STOP')
    client = client_for(Provider())
    with pytest.raises(LLMCallError) as caught:
        client.generate(request(fail_on_truncation=True))
    assert caught.value.failure_code == code
    assert client.tracer.snapshot()['calls'][0]['outcome'] == outcome
    assert client.tracer.snapshot()['contract_failed_calls'] == 1


def test_completed_is_accepted_with_an_unrelated_native_reason():
    result = client_for(MeasuredProvider()).generate(request())
    assert result.response.finish_reason == 'native_success' and result.response.raw == ['opaque']
    assert set(result.response.usage.values()) == {None}


def seasonal_profiles(model='vision-a'):
    evidence = neutral_profile('seasonal-outlook', provider='images-lab', model=model, attempts=2,
                               max_output_tokens=18000, location='visual-region')
    report = replace(evidence, provider='text-lab', model='text-b', location='text-region', max_output_tokens=22000)
    return dict(evidence=evidence, report=report)


def test_seasonal_can_route_profiles_to_different_providers_and_audits_effective_parameters(service, monkeypatch):
    images, text = FakeProvider(), FakeProvider()
    images.name, text.name = 'images-lab', 'text-lab'
    monkeypatch.setattr(clients, 'service_profiles', lambda *args, **kwargs: seasonal_profiles())
    monkeypatch.setattr(clients, 'default_provider', lambda name, **kwargs: {'images-lab': images, 'text-lab': text}[name])
    info = service.info()
    assert info['model'] is None and info['location'] is None and set(info['profiles']) == {'evidence', 'report'}
    run = action(service, prepared(service), 'extract')
    runner.run_phase(service, run['id'], run['active'])
    run = service.get(run['id'])
    run = action(service, run, 'confirm', version_id=run['current_evidence'], confirmed=True)
    runner.run_phase(service, run['id'], run['active'])
    run = service.get(run['id'])
    assert run['status'] == 'completed'
    assert [r['stage'] for r in images.requests] == ['extraction', 'review', 'refinement']
    assert [r['stage'] for r in text.requests] == ['draft', 'report_review', 'redraft']
    for operation in run['operations'].values():
        for call in operation['calls']:
            expected = seasonal_profiles()['report' if operation['phase'] == 'report' else 'evidence']
            assert call['requested_model'] == expected.model and call['provider'] == expected.provider
            assert call['parameters']['max_output_tokens'] == expected.max_output_tokens
            assert call['token_usage']['prompt_tokens'] == 10 and call['token_usage']['total_tokens'] is None
            stored = service.store.json(call['response'])
            assert stored['raw'] == {'opaque': ['uninterpreted', 'diagnostic']}


def test_seasonal_retry_resolves_a_new_snapshot_without_rewriting_the_previous_audit(service, monkeypatch):
    models = ['first']
    resolved = []
    def profiles(*args, **kwargs):
        resolved.append(models[0])
        return seasonal_profiles(models[0])
    monkeypatch.setattr(clients, 'service_profiles', profiles)
    class FailsOnce(FakeProvider):
        name = 'images-lab'
        def generate(self, profile, request):
            models[0] = 'second'
            raise ProviderError('Denied', kind='authentication')
    run = action(service, prepared(service), 'extract')
    old_id = run['active']
    with pytest.raises(LLMCallError):
        runner.run_phase(service, run['id'], old_id, FailsOnce())
    run = service.get(run['id'])
    old = copy.deepcopy(run['operations'][old_id])
    assert len(old['calls']) == 1
    run = action(service, run, 'retry', operation_id=old_id)
    runner.run_phase(service, run['id'], run['active'], FakeProvider())
    run = service.get(run['id'])
    assert run['operations'][old_id] == old and resolved == ['first', 'second']
    assert old['runtime']['profiles']['evidence']['model'] == 'first'
    latest = list(run['operations'].values())[-1]
    assert latest['runtime']['profiles']['evidence']['model'] == 'second'


def test_images_keep_original_bytes_and_notes_in_order(service):
    run = prepared(service)
    import uuid
    second = image_bytes('JPEG')
    run = service.upload(run['id'], dict(request_id=uuid.uuid4().hex, expected_revision=run['revision'],
                         product='seasonal_rain', note='Second image note'), second, 'second.jpg')
    state = engine.initial_state(service.validate(run['id']), run['maps'])
    domain = nodes.request_for('extraction', state)
    req = llm_request(domain, 600, 'test')
    assert len(req.parts) == 5
    for index, original in enumerate((image_bytes(), second)):
        note, part = req.parts[1+2*index:3+2*index]
        assert json.loads(note)['figure_id'] == domain['images'][index]['figure_id']
        assert json.loads(note)['metadata_note'] == domain['images'][index]['metadata_note']
        assert set(part.reference) == {'namespace', 'key', 'sha256', 'size', 'mime'}
        assert service.store.read(part.reference) == original


def test_download_api_uses_storage_expiry_and_shared_delivery_limit(service, monkeypatch):
    from app.api import app
    from app.streamlit_backend.dispatcher import dispatch_request
    monkeypatch.setattr(seasonal_api, 'get_service', lambda: service)
    monkeypatch.setattr(service.store, 'download_link', lambda ref, name: dict(url='https://download.test/file', expires_in=37))
    run = action(service, prepared(service), 'extract')
    path = f"/seasonal-outlook/runs/{run['id']}/input-package"
    assert TestClient(app).get(path).json() == dispatch_request('GET', path).json() == dict(url='https://download.test/file', expires_in=37)
    ref = service.store.put(run['id'], b'report', 'application/zip')
    service.store.mutate(run['id'], lambda r: {**r, 'artifacts': {'report.zip': ref}})
    monkeypatch.setattr(seasonal_api, 'inline_download_limit', lambda _: 3)
    response = TestClient(app).get(f"/seasonal-outlook/runs/{run['id']}/artifacts/report.zip")
    assert response.status_code == 400 and 'download-link' in response.text


@pytest.mark.parametrize('version', [None, 1, 2, 4])
def test_old_shared_records_are_410_in_both_transports_without_writes(run_store, version):
    from app.api import app
    from app.streamlit_backend.dispatcher import dispatch_request
    old = dict(record_version=version, status='running', updated_at=0, artifacts=[], revision=3)
    run_store.runs['old-run'] = copy.deepcopy(old)
    http = TestClient(app)
    for method, path in [('GET','/mfi-drafter/status/old-run'), ('GET','/mfi-drafter/result/old-run'),
                         ('POST','/mfi-drafter/export-docx/old-run'), ('GET','/market-monitor/status/old-run'),
                         ('GET','/market-monitor/result/old-run'), ('POST','/market-monitor/export-docx/old-run'),
                         ('GET','/market-monitor/artifacts/old-run/file')]:
        assert http.request(method, path, json={} if method == 'POST' else None).status_code == 410, path
        assert dispatch_request(method, path, json_body={} if method == 'POST' else None).status_code == 410, path
    assert run_store.runs['old-run'] == old


def test_old_seasonal_runtime_is_410_for_retry_export_and_downloads(service, monkeypatch):
    from app.api import app
    from app.streamlit_backend.dispatcher import dispatch_request
    monkeypatch.setattr(seasonal_api, 'get_service', lambda: service)
    run = prepared(service)
    rid = run['id']
    old = service.store.mutate(rid, lambda r: {**r, 'runtime_contract_version': 1})
    for method, tail in [('GET', ''), ('GET','/input'), ('GET','/maps/0'), ('GET','/input-package'),
                         ('GET','/artifacts/report.docx'), ('GET','/download-link/report.docx'), ('POST','/retry')]:
        path = f'/seasonal-outlook/runs/{rid}' + tail
        data = dict(request_id='retry-old-run', expected_revision=run['revision'], operation_id='old-operation')
        assert TestClient(app).request(method, path, json=data if method == 'POST' else None).status_code == 410
        assert dispatch_request(method, path, json_body=data if method == 'POST' else None).status_code == 410
    assert service.store.get(rid) == old


@pytest.mark.parametrize('version', [None, '1.0', '3.0'])
def test_prior_trace_formats_are_not_interpreted_as_current(version):
    with pytest.raises(ValueError):
        Tracer(service='test', run_id='test', initial=dict(service='test', run_id='test', trace_schema_version=version))


def test_memory_objects_validate_integrity_namespace_and_concurrent_records():
    store = MemoryStore('objects', namespace='logical')
    ref = store.put('test', b'original', 'image/png')
    assert store.put('test', b'original', 'image/png') == ref
    for changes in ({'namespace': 'other'}, {'sha256': '0'*64}, {'size': 123}, {'key': 'objects/../'+ref['sha256']}):
        with pytest.raises(ValueError):
            store.read({**ref, **changes})
    store.mutate('test', lambda _: dict(counter=0))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: store.mutate('test', lambda r: dict(counter=r['counter']+1)), range(100)))
    assert store.get('test')['counter'] == 100
    value = store.get('test'); value['counter'] = 0
    assert store.get('test')['counter'] == 100


def test_complete_workflows_in_a_process_that_forbids_cloud_sdk_imports(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-B', str(root/'tests'/'cloud_free_workflows.py'), str(tmp_path/'child')],
                            cwd=root, env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout[-12000:] + result.stderr[-4000:]
    assert 'CLOUD_FREE_WORKFLOWS_PASSED' in result.stdout
