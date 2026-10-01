"""Immutable objects first, atomic records second. No implicit memory fallback.

One JSON record per run, changed only through `mutate` (a transaction on Firestore), and content-addressed
objects written once. `CloudStore` keeps records in Firestore and objects in GCS; `MemoryStore` keeps both in
this process.
"""
import copy
import hashlib
import json
import re
import threading
from datetime import timedelta
from typing import Any, Callable, Protocol, TypedDict


class ObjectRef(TypedDict):
    namespace: str
    key: str
    sha256: str
    size: int
    mime: str


class RunStore(Protocol):
    namespace: str
    def put(self, run_id: str, data: bytes, mime: str) -> ObjectRef: ...
    def read(self, ref: ObjectRef) -> bytes: ...
    def put_json(self, run_id: str, value: Any) -> ObjectRef: ...
    def json(self, ref: ObjectRef) -> Any: ...
    def get(self, run_id: str) -> dict: ...
    def mutate(self, run_id: str, fn: Callable) -> dict: ...
    def list(self, limit=50, before=None, **filters) -> list[dict]: ...
    def download_link(self, ref: ObjectRef, filename: str) -> dict: ...


class UnsupportedRunVersion(ValueError):
    """Stored data predating this runtime is neither read nor rewritten."""


def require_record_version(record):
    if record.get("record_version") != 3:
        raise UnsupportedRunVersion("This run uses a previous storage format. Create a new run.")
    return record


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


DOWNLOAD_TTL_SECONDS = 600


class Objects:
    def validate_ref(self, ref):
        if not isinstance(ref, dict) or set(ref) != {'namespace', 'key', 'sha256', 'size', 'mime'}:
            raise ValueError('Invalid object reference')
        key = ref['key']
        if (not isinstance(key, str) or not isinstance(ref['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', ref['sha256'])
                or key.rsplit('/', 1)[-1] != ref['sha256']
                or type(ref['size']) is not int or ref['size'] < 0
                or not isinstance(ref['mime'], str) or not ref['mime']):
            raise ValueError('Invalid object checksum, size or content type')
        prefix = self.prefix.rstrip('/') + '/'
        if ref.get('namespace') != self.namespace or not key.startswith(prefix) or any(p in ('', '.', '..') for p in key.split('/')) or '\\' in key:
            raise ValueError("Object outside this store's namespace")

    def download_link(self, ref, filename):
        return dict(url=self.signed_url(ref, filename), expires_in=DOWNLOAD_TTL_SECONDS)

    def put_json(self, run_id, value):
        return self.put(run_id, encode(value), 'application/json')

    def json(self, ref):
        return json.loads(self.read(ref))


class MemoryStore(Objects):
    """Records and objects in this process; never shared with other instances."""
    def __init__(self, prefix, *, missing='Record not found', namespace=None):
        self.namespace = namespace or prefix.strip('/')
        self.prefix, self.missing = prefix.strip('/'), missing
        self.runs, self.objects = {}, {}
        self.lock = threading.RLock()

    def put(self, run_id, data, mime):
        sha = hashlib.sha256(data).hexdigest()
        key = f'{self.prefix}/{run_id}/{sha}'
        with self.lock:
            self.objects[key] = bytes(data)
        return dict(key=key, sha256=sha, size=len(data), mime=mime, namespace=self.namespace)

    def read(self, ref):
        self.validate_ref(ref)
        data = self.objects[ref['key']]
        if len(data) != ref['size'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ValueError('Object checksum mismatch')
        return data

    def download_link(self, ref, filename):
        self.validate_ref(ref)
        raise ValueError('Direct download links are unavailable for in-process memory storage')

    def get(self, run_id):
        with self.lock:
            if run_id not in self.runs:
                raise Missing(self.missing)
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
    """Records in a Firestore collection, objects under a GCS prefix.

    `max_record_bytes` stays below Firestore's 1 MiB document limit; `capacity` is the error a larger record gets.
    `signer` is the service account that signs download links.
    """
    def __init__(self, *, project, database, collection, bucket, prefix, signer=None,
                 max_record_bytes=1_000_000, capacity='Record capacity reached', missing='Record not found', namespace=None):
        self.namespace = namespace or prefix.strip('/')
        from app.shared.cloud import firestore_client, storage_client
        self.db = firestore_client(project, database)
        self.collection = self.db.collection(collection)
        self.bucket = storage_client(project).bucket(bucket)
        self.prefix = prefix.strip('/') + '/'
        self.signer, self.max_record_bytes = signer, max_record_bytes
        self.capacity, self.missing = capacity, missing

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
        return dict(key=key, sha256=sha, size=len(data), mime=mime, namespace=self.namespace)

    def blob(self, ref):
        self.validate_ref(ref)
        return self.bucket.blob(ref['key'])

    def read(self, ref):
        data = self.blob(ref).download_as_bytes()
        if len(data) != ref['size'] or hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ValueError('Object checksum mismatch')
        return data

    def model_uri(self, ref):
        self.read(ref)  # Verify the exact immutable original before handing its location to the model.
        return f"gs://{self.bucket.name}/{ref['key']}"

    def signed_url(self, ref, filename):
        import google.auth
        from google.auth.transport.requests import Request
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        credentials.refresh(Request())
        return self.blob(ref).generate_signed_url(version='v4', expiration=timedelta(seconds=DOWNLOAD_TTL_SECONDS), method='GET',
            service_account_email=self.signer, access_token=credentials.token,
            response_disposition=f'attachment; filename="{filename}"')

    def get(self, run_id):
        value = self.collection.document(run_id).get().to_dict()
        if value is None:
            raise Missing(self.missing)
        return value

    def mutate(self, run_id, fn):
        from google.cloud import firestore
        doc = self.collection.document(run_id)

        @firestore.transactional
        def update(tx):
            current = doc.get(transaction=tx).to_dict()
            value = fn(current)  # No inference or object writes inside a retryable transaction.
            if len(encode(value)) > self.max_record_bytes:
                raise ValueError(self.capacity)
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
