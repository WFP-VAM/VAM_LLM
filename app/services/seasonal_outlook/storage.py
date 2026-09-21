"""Immutable objects first, atomic manifests second. No implicit memory fallback."""
import copy
import hashlib
import json
import threading
from datetime import timedelta


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


class Objects:
    def put_json(self, run_id, value):
        return self.put(run_id, encode(value), 'application/json')

    def json(self, ref):
        return json.loads(self.read(ref))


class MemoryStore(Objects):
    """Dependency-injected test store only; never selected by environment configuration."""
    def __init__(self):
        self.runs, self.objects = {}, {}
        self.lock = threading.RLock()

    def put(self, run_id, data, mime):
        sha = hashlib.sha256(data).hexdigest()
        key = f'seasonal-outlook/{run_id}/{sha}'
        with self.lock:
            self.objects[key] = bytes(data)
        return dict(key=key, sha256=sha, size=len(data), mime=mime, uri='gs://test/' + key)

    def read(self, ref):
        data = self.objects[ref['key']]
        if hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ValueError('Object checksum mismatch')
        return data

    def get(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise Missing('Analysis not found')
            return copy.deepcopy(self.runs[run_id])

    def mutate(self, run_id, fn):
        with self.lock:
            current = copy.deepcopy(self.runs.get(run_id))
            updated = fn(current)
            self.runs[run_id] = copy.deepcopy(updated)
            return copy.deepcopy(updated)

    def list(self, limit=50, before=None, **filters):
        with self.lock:
            values = [copy.deepcopy(v) for v in self.runs.values()
                      if all(not x or v.get(k) == x for k, x in filters.items())
                      and (not before or v['created_at'] < before)]
        return sorted(values, key=lambda x: x['created_at'], reverse=True)[:limit]


class CloudStore(Objects):
    def __init__(self, settings):
        from google.cloud import firestore, storage
        if not settings.project or not settings.bucket:
            raise ValueError('Explicit Seasonal project and bucket are required for persistence')
        self.settings = settings
        self.db = firestore.Client(project=settings.project, database=settings.database)
        self.collection = self.db.collection(settings.collection)
        self.bucket = storage.Client(project=settings.project).bucket(settings.bucket)
        self.prefix = settings.prefix.strip('/') + '/'

    def put(self, run_id, data, mime):
        from google.api_core.exceptions import PreconditionFailed
        sha = hashlib.sha256(data).hexdigest()
        key = f'{self.prefix}{run_id}/{sha}'
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_string(data, content_type=mime, if_generation_match=0, checksum='auto')
        except PreconditionFailed:
            if hashlib.sha256(blob.download_as_bytes()).hexdigest() != sha:
                raise ValueError('Immutable object collision')
        return dict(key=key, sha256=sha, size=len(data), mime=mime, uri=f'gs://{self.bucket.name}/{key}')

    def blob(self, ref):
        if not ref['key'].startswith(self.prefix) or '..' in ref['key'].split('/'):
            raise ValueError('Object outside Seasonal namespace')
        return self.bucket.blob(ref['key'])

    def read(self, ref):
        data = self.blob(ref).download_as_bytes()
        if hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ValueError('Object checksum mismatch')
        return data

    def signed_url(self, ref, filename):
        import google.auth
        from google.auth.transport.requests import Request
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        credentials.refresh(Request())
        return self.blob(ref).generate_signed_url(version='v4', expiration=timedelta(minutes=10), method='GET',
            service_account_email=self.settings.signer, access_token=credentials.token,
            response_disposition=f'attachment; filename="{filename}"')

    def get(self, run_id):
        value = self.collection.document(run_id).get().to_dict()
        if value is None:
            raise Missing('Analysis not found')
        return value

    def mutate(self, run_id, fn):
        from google.cloud import firestore
        doc = self.collection.document(run_id)

        @firestore.transactional
        def update(tx):
            current = doc.get(transaction=tx).to_dict()
            value = fn(current)  # No inference or object writes inside a retryable transaction.
            if len(encode(value)) > 800_000:
                raise ValueError('Analysis audit capacity reached; create a new analysis')
            tx.set(doc, value)
            return value
        return update(self.db.transaction())

    def list(self, limit=50, before=None, **filters):
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        query = self.collection
        for key, value in filters.items():
            if value:
                query = query.where(filter=FieldFilter(key, '==', value))
        if before:
            query = query.where(filter=FieldFilter('created_at', '<', before))
        return [d.to_dict() for d in query.order_by('created_at', direction=firestore.Query.DESCENDING).limit(limit).stream()]
