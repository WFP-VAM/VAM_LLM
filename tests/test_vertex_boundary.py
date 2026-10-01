"""Adapter capabilities, schema policies, normalized errors and immutable configuration."""
import copy
import json
from dataclasses import replace

import httpx
import pytest
from google.genai import types
from app.shared.llm import FilePart, LLMRequest, ProviderError, create_llm_client
from app.shared.llm.profiles import mfi_profile, service_profiles
from app.shared.llm.vertex import VertexProvider, reply_from, provider_error
from app.shared.llm.vertex_schema import compile_schema, SchemaConfigurationError
from app.shared.seasonal import SeasonalSettings
from test_vertex_seasonal import OriginalImages
from app.shared.runs.store import MemoryStore


def req(**kwargs):
    return LLMRequest(**{**dict(operation='mfi.light.draft_dimensions.v1', node='draft_dimensions', parts=['é test'],
                               json_output=True, response_schema={'type':'object', 'properties':{'b':{'type':'integer'}, 'a':{'type':'string'}}}), **kwargs})


@pytest.mark.parametrize('reason,outcome', [('STOP','completed'), ('MAX_TOKENS','truncated'), ('SAFETY','blocked'),
                                         ('RECITATION','blocked'), ('OTHER','unknown')])
def test_finish_reasons_are_normalized_and_retained(reason, outcome):
    response = types.GenerateContentResponse.model_validate(dict(candidates=[dict(finish_reason=reason,
                  content=dict(parts=[dict(text='answer')]))]))
    result = reply_from(response)
    assert result.outcome == outcome and result.finish_reason == reason
    assert result.usage == dict(prompt_tokens=None,candidate_tokens=None,thought_tokens=None,total_tokens=None)


def test_prompt_block_is_not_confused_with_a_missing_finish_reason():
    response = types.GenerateContentResponse.model_validate(dict(prompt_feedback=dict(block_reason='SAFETY')))
    assert reply_from(response).outcome == 'blocked'
    assert reply_from(types.GenerateContentResponse()).outcome == 'unknown'


@pytest.mark.parametrize('status,kind,transient', [(400,'request',False),(403,'authentication',False),
                                                 (429,'transport',True),(503,'transport',True)])
def test_count_sdk_errors_use_the_same_normalized_categories_as_inference(vertex_wire,status,kind,transient):
    calls,replies=vertex_wire
    replies.append(httpx.Response(status,json=dict(error=dict(code=status,message='synthetic'))))
    with pytest.raises(ProviderError) as exc:
        VertexProvider().count_tokens(replace(mfi_profile(),project='offline'), req())
    assert (exc.value.kind,exc.value.transient)==(kind,transient) and len(calls)==1


def test_missing_vertex_token_count_is_incompatible_not_zero(vertex_wire):
    calls,replies=vertex_wire
    replies.append(httpx.Response(200,json={}))
    with pytest.raises(ProviderError,match='did not return'):
        VertexProvider().count_tokens(replace(mfi_profile(),project='offline'),req())
    assert len(calls)==1


def test_credentials_errors_are_not_content_or_transient_failures():
    from google.auth.exceptions import DefaultCredentialsError,RefreshError
    for error in (DefaultCredentialsError('missing'),RefreshError('expired')):
        normalized=provider_error(error)
        assert normalized.kind=='authentication' and not normalized.transient


def test_project_and_model_configuration_stays_frozen_during_a_client_lifetime(vertex_wire,monkeypatch):
    calls,replies=vertex_wire
    monkeypatch.setenv('VERTEX_PROJECT_ID','first-project')
    monkeypatch.setenv('LLM_MODEL','first-model')
    client=create_llm_client('market-monitor',provider=VertexProvider())
    monkeypatch.setenv('VERTEX_PROJECT_ID','second-project')
    monkeypatch.setenv('LLM_MODEL','second-model')
    client.generate(LLMRequest(operation='test',node='n',parts=['test']))
    assert '/projects/first-project/' in calls[0]['url'] and '/models/first-model:' in calls[0]['url']
    assert client.describe()['profiles']['text']['project']=='first-project'
    other=create_llm_client('market-monitor',provider=VertexProvider())
    assert other.describe()['model']=='second-model' and other.describe()['profiles']['text']['project']=='second-project'


def test_seasonal_resolves_environment_again_for_each_new_operation(monkeypatch):
    monkeypatch.setenv('SEASONAL_PROJECT','test-project')
    monkeypatch.setenv('SEASONAL_LOCATION','global')
    monkeypatch.setenv('SEASONAL_MODEL','first-model')
    settings=SeasonalSettings.from_env()
    first=service_profiles('seasonal-outlook',settings=settings)
    monkeypatch.setenv('SEASONAL_MODEL','second-model')
    second=service_profiles('seasonal-outlook',settings=settings)
    assert first['evidence'].model=='first-model' and second['evidence'].model=='second-model'


def test_vertex_measurement_preserves_character_calculation_and_schema_order(vertex_wire):
    calls,replies=vertex_wire
    replies.append(httpx.Response(200,json=dict(totalTokens=99)))
    profile=replace(mfi_profile(),project='offline')
    client=create_llm_client('mfi-drafter',profiles=dict(text=profile,summary=replace(profile,timeout_seconds=180)),provider=VertexProvider())
    request=req()
    compiled=compile_schema(request.response_schema,profile)
    old=dict(model=profile.model,contents=[dict(role='user',parts=[dict(text=request.parts[0])])],
             generation_config=dict(response_schema=compiled,response_mime_type='application/json',temperature=1.0,max_output_tokens=65536))
    measured=client.measure(request)
    assert measured.characters==len(json.dumps(old,ensure_ascii=False,sort_keys=True,separators=(',',':')))
    assert measured.input_tokens==99 and calls[0]['timeout']==30
    assert compiled['propertyOrdering']==['a','b']


def test_schema_cache_is_bound_to_policy_model_and_preserves_local_contract():
    profile=mfi_profile()
    standard=req().response_schema
    standard['properties']['a'].update(minLength=4,maxLength=8)
    original=copy.deepcopy(standard)
    alphabetical=compile_schema(standard,profile)
    declaration=compile_schema(standard,replace(profile,schema_policy='declaration'))
    assert alphabetical['propertyOrdering']==['a','b'] and declaration['propertyOrdering']==['b','a']
    assert 'minLength' not in alphabetical['properties']['a'] and standard==original
    alphabetical['properties'].clear()
    assert compile_schema(standard,replace(profile,model='different'))['properties']


@pytest.mark.parametrize('policy',['mfi','declaration'])
@pytest.mark.parametrize('schema',[
    {'oneOf':[{'type':'string'},{'type':'integer'}]},
    {'type':'object','properties':{'a':{'type':'string'}},'additionalProperties':{'type':'integer'}},
    {'$defs':{'recursive':{'$ref':'#/$defs/recursive'}},'$ref':'#/$defs/recursive'},
    {'$ref':'https://example.test/schema'},
    {'type':'invalid'}])
def test_unsupported_structural_schema_fails_before_inference(vertex_wire,policy,schema):
    calls,_=vertex_wire
    profile=replace(mfi_profile(),project='offline',schema_policy=policy)
    client=create_llm_client('mfi-drafter',profiles=dict(text=profile,summary=profile),provider=VertexProvider())
    with pytest.raises(SchemaConfigurationError):
        client.generate(req(response_schema=schema))
    assert calls==[]


def test_contract_and_transport_hashes_are_separate_and_copy_safe(vertex_wire):
    calls,_=vertex_wire
    profile=replace(mfi_profile(),project='offline')
    client=create_llm_client('mfi-drafter',profiles=dict(text=profile,summary=profile),provider=VertexProvider())
    client.generate(req())
    trace=client.tracer.snapshot()['calls'][0]
    assert trace['contract_schema_hash']!=trace['transport_schema_hash']
    from app.shared.llm.client import _digest
    assert trace['transport_schema_hash'] == _digest(calls[0]['body']['generationConfig']['responseSchema'])
    assert trace['provider']=='vertex_ai' and trace['outcome']=='completed'
    assert trace['parameters']['max_output_tokens']==65536


@pytest.mark.parametrize('mime,size',[('application/pdf',10),('image/png',30000001)])
def test_unsupported_original_images_fail_without_inference(vertex_wire,mime,size):
    calls,_=vertex_wire
    store=MemoryStore('originals',namespace='images')
    ref=store.put('run',b'original','image/png')
    ref.update(mime=mime,size=size)
    with pytest.raises(ProviderError,match='Unsupported original'):
        VertexProvider(store=OriginalImages(store)).generate(replace(mfi_profile(),project='offline'),req(parts=[FilePart(ref)]))
    assert calls==[]


def test_provider_schemas_match_the_pre_refactor_baseline():
    from pathlib import Path
    from app.services.mfi_drafter.contracts import SectionsResponse, ReviewResponse
    from test_seasonal_outlook import Settings, Service, Launcher, prepared, engine, nodes, FakeProvider, reply
    from app.shared.llm.client import _digest
    baseline=json.loads((Path(__file__).parent/'fixtures'/'provider_schema_baseline.json').read_text())['schemas']
    for name,model in [('mfi_sections',SectionsResponse),('mfi_review',ReviewResponse)]:
        assert _digest(compile_schema(model.model_json_schema(),mfi_profile()))==baseline[name]
    service=Service(Settings(project='test',bucket='test',signer='test'),MemoryStore('seasonal-outlook'),Launcher())
    run=prepared(service)
    state=engine.initial_state(service.validate(run['id']),run['maps'])
    fake=FakeProvider()
    for stage in ('extraction','review','refinement','feedback','draft','report_review','redraft'):
        if stage=='feedback': state['analyst_comments']='Can we read an exact number?'
        request=nodes.request_for(stage,state)
        profile=service_profiles('seasonal-outlook',settings=service.settings)['evidence']
        assert _digest(compile_schema(request['schema'],profile))==baseline['seasonal_'+stage]
        state=nodes.accept(stage,state,reply(fake,request),'baseline')


def test_missing_project_is_frozen_but_does_not_break_nodes_that_skip_inference(monkeypatch):
    from app.shared.llm import vertex
    def missing(): raise RuntimeError('No project')
    monkeypatch.setattr(vertex,'resolve_project',missing)
    client=create_llm_client('market-monitor',provider=VertexProvider())
    assert client.describe()['profiles']['text']['project'] is None
    monkeypatch.setenv('VERTEX_PROJECT_ID','too-late')
    from app.shared.llm import LLMCallError
    with pytest.raises(LLMCallError) as error:
        client.generate(LLMRequest(operation='test',node='test',parts=['test']))
    assert error.value.failure_code=='llm_configuration_error' and not error.value.transient


def test_measure_accepts_original_images_and_system_instructions(vertex_wire):
    calls,replies=vertex_wire
    replies.append(httpx.Response(200,json=dict(totalTokens=500)))
    store=MemoryStore('maps',namespace='images')
    ref=store.put('run',b'original','image/png')
    profile=replace(mfi_profile(),project='offline')
    client=create_llm_client('mfi-drafter',profiles=dict(text=profile,summary=profile),
                             provider=VertexProvider(store=OriginalImages(store)))
    request=req(parts=['note',FilePart(ref)],system='image instructions')
    measured=client.measure(request)
    assert measured.allowed and measured.input_tokens==500
    assert calls[0]['body']['systemInstruction']['parts']==[{'text':'image instructions'}]
    assert 'fileData' in calls[0]['body']['contents'][0]['parts'][1]
    assert store.read(ref)==b'original'
