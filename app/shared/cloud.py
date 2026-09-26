"""Google Cloud clients for the whole app: one Firestore client per project and database, one Storage client per project.

A client that fails to start (for example without credentials) is not cached, so the next call tries again.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional, Tuple


@lru_cache(maxsize=None)
def firestore_client(project: Optional[str] = None, database: Optional[str] = None) -> Any:
    from google.cloud import firestore

    return firestore.Client(project=project, database=database)


@lru_cache(maxsize=None)
def storage_client(project: Optional[str] = None) -> Any:
    from google.cloud import storage

    return storage.Client(project=project)


def parse_gcs_uri(uri: str, *, name: str = "GCS URI") -> Tuple[str, str]:
    """The bucket and the object path (or prefix, without surrounding slashes) of a gs:// URI."""
    uri = (uri or "").strip()
    if not uri.startswith("gs://"):
        raise ValueError(f"{name} must start with gs://")
    bucket, _, path = uri[5:].partition("/")
    if not bucket:
        raise ValueError(f"{name} must name a bucket")
    return bucket, path.strip("/")


def read_gcs_uri(uri: str) -> bytes:
    """The bytes of the object at a gs:// URI, read with the default project's credentials."""
    bucket, path = parse_gcs_uri(uri)
    return storage_client().bucket(bucket).blob(path).download_as_bytes()
