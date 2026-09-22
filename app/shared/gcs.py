"""Small Google Cloud Storage helpers shared by the drafting services."""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Tuple

logger = logging.getLogger(__name__)

_GCS_CLIENT_LOCK = threading.Lock()
_GCS_CLIENT: Any = None


def parse_gcs_uri(uri: str) -> Tuple[str, str]:
    uri = (uri or "").strip()
    if not uri.startswith("gs://"):
        raise ValueError("Invalid GCS URI")
    path = uri[5:]
    bucket, _, obj = path.partition("/")
    return bucket, obj


def get_gcs_client() -> Any:
    global _GCS_CLIENT
    if _GCS_CLIENT is not None:
        return _GCS_CLIENT
    with _GCS_CLIENT_LOCK:
        if _GCS_CLIENT is not None:
            return _GCS_CLIENT
        try:
            from google.cloud import storage  # type: ignore

            _GCS_CLIENT = storage.Client()
        except Exception:
            logger.exception("Failed to initialize GCS client")
            _GCS_CLIENT = None
        return _GCS_CLIENT


def download_gcs_to_file(gcs_uri: str, destination: Path) -> None:
    client = get_gcs_client()
    if client is None:
        raise FileNotFoundError("GCS client is not available")

    bucket_name, object_name = parse_gcs_uri(gcs_uri)
    if not bucket_name or not object_name:
        raise FileNotFoundError("Invalid GCS URI")

    destination.parent.mkdir(parents=True, exist_ok=True)
    client.bucket(bucket_name).blob(object_name).download_to_filename(str(destination))
