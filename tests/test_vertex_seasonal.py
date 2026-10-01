"""Seasonal Vertex serialization and transport policy."""
import json
from dataclasses import replace
import pytest
from app.shared.llm import create_llm_client, LLMCallError
from app.services.seasonal_outlook import engine, nodes
from app.services.seasonal_outlook.calls import llm_request
from test_seasonal_outlook import service, prepared, image_bytes, FakeProvider, reply

class OriginalImages:
    def __init__(self, store):
        self.store = store

    def model_uri(self, reference):
        self.store.validate_ref(reference)
        self.store.read(reference)
        return 'gs://company-test/' + reference['key']


def test_gemini_configuration_and_gcs_images(service, vertex_wire):
    from app.shared.llm.vertex import VertexProvider
    sent, _ = vertex_wire
    run = prepared(service)
    state = engine.initial_state(service.validate(run['id']), run['maps'])
    request = nodes.request_for('extraction', state)
    llm = create_llm_client("seasonal-outlook", settings=service.settings, timeout=1200, provider=VertexProvider(store=OriginalImages(service.store)))
    response = llm.generate(llm_request(request, 1200, 'test')).response
    call = sent[0]
    assert '/projects/company-test/locations/global/publishers/google/models/gemini-3.1-pro-preview:generateContent' in call['url']
    config = call['body']['generationConfig']
    assert config['temperature'] == 1.0 and config['maxOutputTokens'] == 32768
    thinking = config['thinkingConfig']
    assert thinking.get('thinkingLevel', thinking.get('thinking_level')) == 'HIGH'
    assert config['mediaResolution'] == 'MEDIA_RESOLUTION_HIGH'
    assert call['timeout'] == 1200.0 and call['headers']['X-Vertex-AI-LLM-Request-Type'] == 'shared'
    file_data = call['body']['contents'][0]['parts'][-1]['fileData']
    assert file_data.get('fileUri', file_data.get('file_uri')).startswith('gs://')
    assert response.finish_reason == 'STOP' and response.text == 'ok'


def test_no_workstation_project_and_only_original_gcs_images(service):
    with pytest.raises(ValueError, match='Explicit Seasonal project required'):
        create_llm_client('seasonal-outlook', settings=replace(service.settings, project=''))
    run = prepared(service)
    request = nodes.request_for('extraction', engine.initial_state(service.validate(run['id']), run['maps']))
    reference = request['images'][0]['object']
    assert 'uri' not in reference
    original = service.store.read(reference)
    assert original == image_bytes()
    reference['namespace'] = 'other'
    with pytest.raises(ValueError, match='namespace'):
        OriginalImages(service.store).model_uri(reference)


def test_real_sdk_wire_conversion_without_network(service, monkeypatch):
    """Exercise the actual SDK's schema/URI serialization with a local HTTP transport."""
    import httpx
    from google import genai
    from google.oauth2.credentials import Credentials
    from app.shared.llm.vertex import VertexProvider
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
    llm = create_llm_client("seasonal-outlook", settings=service.settings, timeout=600, provider=VertexProvider(store=OriginalImages(service.store)))
    fake = FakeProvider()
    for stage in ('extraction', 'review', 'refinement', 'draft', 'report_review', 'redraft'):
        request = nodes.request_for(stage, state)
        wire_response = dict(candidates=[dict(content=dict(role='model', parts=[dict(text=reply(fake, request)['text'])]), finishReason='STOP')],
                             modelVersion='gemini-3.1-pro-preview', usageMetadata=dict(promptTokenCount=10, candidatesTokenCount=20))
        result = llm.generate(llm_request(request, 600, 'wire-test')).response
        state = nodes.accept(stage, state, dict(text=result.text, outcome=result.outcome), 'wire-test')
        from app.shared.llm.client import _digest
        assert llm.tracer.snapshot()['calls'][-1]['transport_schema_hash'] == _digest(calls[-1]['generationConfig']['responseSchema'])
    assert len(calls) == 6
    assert calls[0]['generationConfig']['maxOutputTokens'] == 32768
    assert calls[-1]['generationConfig']['maxOutputTokens'] == 65536
    assert 'fileData' in calls[0]['contents'][0]['parts'][-1]
    assert len(calls[-1]['contents'][0]['parts']) == 1


@pytest.mark.parametrize('status,code,sent', [(503, 'UNAVAILABLE', 2), (403, 'PERMISSION_DENIED', 1)])
def test_the_client_retries_a_server_error_once_and_never_a_refusal(service, vertex_wire, status, code, sent):
    import httpx
    from app.shared.llm.vertex import VertexProvider
    calls, replies = vertex_wire
    replies += [httpx.Response(status, json={'error': {'code': status, 'message': 'Synthetic', 'status': code}})] * 2
    run = prepared(service)
    request = nodes.request_for('extraction', engine.initial_state(service.validate(run['id']), run['maps']))
    llm = create_llm_client("seasonal-outlook", settings=service.settings, timeout=600, provider=VertexProvider(store=OriginalImages(service.store)), sleep=lambda _seconds: None)
    with pytest.raises(LLMCallError) as caught:
        llm.generate(llm_request(request, 600, 'test'))
    assert (caught.value.failure_code, caught.value.transient) == ('llm_transport_error' if status == 503 else 'llm_authentication_error', status == 503)
    assert len(calls) == sent  # The SDK itself never retries.
