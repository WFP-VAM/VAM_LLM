"""GCP persistence adapter, against transactional/object-storage doubles; never real credentials."""
import copy
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from app.shared import cloud
from app.shared.runs.store import CloudStore, DOWNLOAD_TTL_SECONDS
from app.shared.runs.factory import create_store
from app.shared.seasonal import SeasonalSettings


@pytest.fixture
def gcp(monkeypatch):
    from google.cloud import firestore
    from google.api_core.exceptions import PreconditionFailed
    lock=threading.RLock()
    records,objects,uploads,signatures={},{},[],[]
    class Doc:
        def __init__(self,key): self.key=key
        def get(self,transaction=None): return SimpleNamespace(to_dict=lambda:copy.deepcopy(records.get(self.key)))
    class Tx:
        def set(self,doc,value): records[doc.key]=copy.deepcopy(value)
    def transactional(fn):
        def wrapped(tx):
            with lock: return fn(tx)
        return wrapped
    monkeypatch.setattr(firestore,'transactional',transactional)
    class Blob:
        def __init__(self,key): self.key=key
        def upload_from_string(self,data,**kwargs):
            uploads.append(kwargs)
            if self.key in objects: raise PreconditionFailed('already exists')
            objects[self.key]=bytes(data)
        def download_as_bytes(self): return objects[self.key]
        def generate_signed_url(self,**kwargs):
            signatures.append(kwargs)
            return 'https://signed.test/object'
    bucket=SimpleNamespace(name='test-bucket',blob=Blob)
    db=SimpleNamespace(collection=lambda _:SimpleNamespace(document=Doc),transaction=Tx)
    monkeypatch.setattr(cloud,'firestore_client',lambda project,database:db)
    monkeypatch.setattr(cloud,'storage_client',lambda project:SimpleNamespace(bucket=lambda name:bucket))
    store=create_store('seasonal-outlook',settings=SeasonalSettings(project='test',bucket='test-bucket',signer='test-signer'))
    return SimpleNamespace(store=store,records=records,objects=objects,uploads=uploads,signatures=signatures)


def test_immutable_gcs_objects_are_content_addressed_and_checksum_checked(gcp):
    ref=gcp.store.put('run',b'original','image/png')
    assert ref==gcp.store.put('run',b'original','image/png')
    assert 'uri' not in ref and ref['namespace']=='seasonal-outlook'
    assert all(item['if_generation_match']==0 for item in gcp.uploads)
    assert gcp.store.model_uri(ref)=='gs://test-bucket/'+ref['key']
    assert gcp.store.read(ref)==b'original'
    with pytest.raises(ValueError,match='namespace'):
        gcp.store.model_uri({**ref,'namespace':'another-store'})
    gcp.objects[ref['key']]=b'changed'
    with pytest.raises(ValueError,match='checksum'):
        gcp.store.read(ref)
    with pytest.raises(ValueError,match='checksum'):
        gcp.store.model_uri(ref)
    with pytest.raises(ValueError,match='collision'):
        gcp.store.put('run',b'original','image/png')


def test_firestore_transactions_prevent_lost_updates_and_reject_oversize_records(gcp):
    gcp.store.mutate('run',lambda _:dict(counter=0))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _:gcp.store.mutate('run',lambda r:dict(counter=r['counter']+1)),range(100)))
    assert gcp.store.get('run')['counter']==100 and gcp.store.max_record_bytes==800000
    with pytest.raises(ValueError,match='capacity'):
        gcp.store.mutate('run',lambda r:{**r,'large':'x'*800000})
    assert gcp.store.get('run')==dict(counter=100)
    with pytest.raises(RuntimeError):
        def abort(record):
            record['counter']=-1
            raise RuntimeError('abort transaction')
        gcp.store.mutate('run',abort)
    assert gcp.store.get('run')['counter']==100


def test_signed_download_expiry_matches_the_url_signature(gcp,monkeypatch):
    import google.auth
    credentials=SimpleNamespace(token='test',refresh=lambda _:None)
    monkeypatch.setattr(google.auth,'default',lambda **kwargs:(credentials,'test'))
    ref=gcp.store.put('run',b'file','application/zip')
    link=gcp.store.download_link(ref,'report.zip')
    assert link==dict(url='https://signed.test/object',expires_in=DOWNLOAD_TTL_SECONDS)
    assert gcp.signatures[0]['expiration'].total_seconds()==link['expires_in']
    assert gcp.signatures[0]['service_account_email']=='test-signer'


def test_seasonal_durable_store_has_no_memory_fallback(monkeypatch):
    from app.shared.runs import factory
    for settings in [SeasonalSettings(),SeasonalSettings(project='p',bucket='b',backend='memory')]:
        with pytest.raises(ValueError): create_store('seasonal-outlook',settings=settings)
    def fail(**kwargs): raise RuntimeError('storage unavailable')
    monkeypatch.setattr(factory,'CloudStore',fail)
    with pytest.raises(RuntimeError,match='storage unavailable'):
        create_store('seasonal-outlook',settings=SeasonalSettings(project='p',bucket='b'))
