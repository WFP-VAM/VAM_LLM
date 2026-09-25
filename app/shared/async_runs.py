from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

RunStatus = Literal["pending", "running", "completed", "failed"]

_UNSET = object()

logger = logging.getLogger(__name__)

@dataclass
class RunArtifactDescriptor:
    artifact_id: str
    label: str
    mime_type: str
    file_name: str
    download_path: str


@dataclass
class RunArtifact(RunArtifactDescriptor):
    content: bytes = b""
    storage_uri: Optional[str] = None
    inline_content_b64: Optional[str] = None


@dataclass
class RunRecord:
    status: RunStatus = "pending"
    current_node: Optional[str] = None
    progress_pct: int = 0
    warnings: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[RunArtifactDescriptor] = field(default_factory=list)
    error: Optional[str] = None
    traceback: Optional[str] = None
    result: Any = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class RunStoreUnavailable(RuntimeError):
    """Durable run storage is configured but cannot be written, so the run is not started."""


_RUNS: Dict[str, RunRecord] = {}
_RUN_ARTIFACTS: Dict[str, Dict[str, RunArtifact]] = {}
_LOCK = threading.Lock()

_BACKEND: Optional[str] = None
_GCP_CLIENTS_LOCK = threading.Lock()
_FIRESTORE_CLIENT: Any = None
_STORAGE_CLIENT: Any = None
_INLINE_RESULT_MAX_BYTES = 900_000
_INLINE_ARTIFACT_MAX_BYTES = 200_000

def _parse_gcs_uri(uri: str) -> Tuple[str, str]:
    uri = (uri or "").strip()
    if not uri.startswith("gs://"):
        raise ValueError("Invalid GCS URI")
    path = uri[5:]
    bucket, _, prefix = path.partition("/")
    return bucket, prefix.strip("/")

def _json_default(obj: Any) -> Any:
    if obj is None:
        return None

    if isinstance(obj, (list, dict, str, int, float, bool)):
        return obj

    if isinstance(obj, (set, tuple)):
        return list(obj)

    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return str(obj)

    iso = getattr(obj, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass

    try:
        import numpy as np  # type: ignore

        if isinstance(obj, np.generic):
            return obj.item()
    except Exception:
        pass

    try:
        import pandas as pd  # type: ignore

        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
    except Exception:
        pass

    return str(obj)

def _has_gcp_deps() -> bool:
    try:
        import google.cloud.firestore  # type: ignore
        import google.cloud.storage  # type: ignore

        return True
    except Exception:
        return False

def _select_backend() -> str:
    global _BACKEND
    if _BACKEND is not None:
        return _BACKEND

    backend_env = (os.getenv("RUNS_BACKEND") or "").strip().lower()
    if backend_env in {"firestore_gcs", "firestore", "gcs"}:
        backend = "firestore_gcs"
    elif backend_env in {"memory", ""}:
        backend = "memory"
    else:
        backend = "memory"

    if backend_env == "" and (os.getenv("RUNS_GCS_URI") or "").strip():
        backend = "firestore_gcs"

    if backend == "firestore_gcs":
        runs_gcs_uri = (os.getenv("RUNS_GCS_URI") or "").strip()
        if not runs_gcs_uri:
            logger.warning("RUNS_GCS_URI is not set; falling back to in-memory run store")
            backend = "memory"
        elif not _has_gcp_deps():
            logger.warning("GCP client libraries are not available; falling back to in-memory run store")
            backend = "memory"

    _BACKEND = backend
    return _BACKEND

def _use_durable_store() -> bool:
    return _select_backend() == "firestore_gcs"

def _get_firestore_database() -> Optional[str]:
    value = (os.getenv("RUNS_FIRESTORE_DATABASE") or "").strip()
    if value:
        return value
    return "vam-llm-async"

def _get_firestore_client() -> Any:
    global _FIRESTORE_CLIENT
    if _FIRESTORE_CLIENT is not None:
        return _FIRESTORE_CLIENT
    with _GCP_CLIENTS_LOCK:
        if _FIRESTORE_CLIENT is not None:
            return _FIRESTORE_CLIENT
        try:
            from google.cloud import firestore  # type: ignore

            database = _get_firestore_database()
            if database:
                try:
                    _FIRESTORE_CLIENT = firestore.Client(database=database)
                except TypeError:
                    _FIRESTORE_CLIENT = firestore.Client()
            else:
                _FIRESTORE_CLIENT = firestore.Client()
        except Exception:
            logger.exception("Failed to initialize Firestore client")
            _FIRESTORE_CLIENT = None
        return _FIRESTORE_CLIENT

def _get_storage_client() -> Any:
    global _STORAGE_CLIENT
    if _STORAGE_CLIENT is not None:
        return _STORAGE_CLIENT
    with _GCP_CLIENTS_LOCK:
        if _STORAGE_CLIENT is not None:
            return _STORAGE_CLIENT
        try:
            from google.cloud import storage  # type: ignore

            _STORAGE_CLIENT = storage.Client()
        except Exception:
            logger.exception("Failed to initialize GCS client")
            _STORAGE_CLIENT = None
        return _STORAGE_CLIENT

def _get_firestore_collection() -> str:
    return (os.getenv("RUNS_FIRESTORE_COLLECTION") or "async_runs").strip() or "async_runs"

def _get_runs_gcs_bucket_prefix() -> Tuple[str, str]:
    uri = (os.getenv("RUNS_GCS_URI") or "").strip()
    bucket, prefix = _parse_gcs_uri(uri)
    if not prefix:
        prefix = "runs"
    return bucket, prefix

def _firestore_doc_ref(run_id: str) -> Any:
    client = _get_firestore_client()
    if client is None:
        return None
    collection = _get_firestore_collection()
    return client.collection(collection).document(run_id)

def _serialize_result_json_bytes(result: Any) -> Optional[bytes]:
    try:
        return json.dumps(result, ensure_ascii=False, default=_json_default).encode("utf-8")
    except Exception:
        logger.exception("Failed to serialize run result")
        return None

def _normalize_artifact_content(content: Any) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    payload = _serialize_result_json_bytes(content)
    if payload is None:
        raise ValueError("Artifact content is not serializable")
    return payload

def _public_artifact_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, RunArtifact):
        item = RunArtifactDescriptor(
            artifact_id=item.artifact_id,
            label=item.label,
            mime_type=item.mime_type,
            file_name=item.file_name,
            download_path=item.download_path,
        )
    if isinstance(item, RunArtifactDescriptor):
        return asdict(item)
    if isinstance(item, dict):
        return {
            "artifact_id": str(item.get("artifact_id") or ""),
            "label": str(item.get("label") or ""),
            "mime_type": str(item.get("mime_type") or "application/octet-stream"),
            "file_name": str(item.get("file_name") or "download.bin"),
            "download_path": str(item.get("download_path") or ""),
        }
    return {
        "artifact_id": "",
        "label": "",
        "mime_type": "application/octet-stream",
        "file_name": "download.bin",
        "download_path": "",
    }

def _artifact_descriptor_from_dict(item: Dict[str, Any]) -> RunArtifactDescriptor:
    public_item = _public_artifact_dict(item)
    return RunArtifactDescriptor(
        artifact_id=public_item["artifact_id"],
        label=public_item["label"],
        mime_type=public_item["mime_type"],
        file_name=public_item["file_name"],
        download_path=public_item["download_path"],
    )

def _upload_run_artifact(
    run_id: str,
    artifact_id: str,
    file_name: str,
    content: bytes,
    *,
    mime_type: str,
) -> Optional[str]:
    client = _get_storage_client()
    if client is None:
        return None

    try:
        bucket_name, prefix = _get_runs_gcs_bucket_prefix()
    except Exception:
        logger.exception("Failed to read RUNS_GCS_URI for artifact upload")
        return None

    object_name = "/".join([p for p in [prefix, run_id, "artifacts", artifact_id, file_name] if p])
    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(content, content_type=mime_type)
        return f"gs://{bucket_name}/{object_name}"
    except Exception:
        logger.exception("Failed to upload run artifact to GCS")
        return None

def _download_artifact_bytes(uri: str) -> Optional[bytes]:
    client = _get_storage_client()
    if client is None:
        return None

    try:
        bucket_name, object_name = _parse_gcs_uri(uri)
    except Exception:
        logger.exception("Invalid artifact GCS URI")
        return None

    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        return blob.download_as_bytes()
    except Exception:
        logger.exception("Failed to download run artifact from GCS")
        return None

def _try_store_result_inline(doc_ref: Any, run_id: str, result: Any) -> bool:
    payload = _serialize_result_json_bytes(result)
    if payload is None:
        return False
    if len(payload) > _INLINE_RESULT_MAX_BYTES:
        return False

    try:
        inline_obj = json.loads(payload.decode("utf-8"))
    except Exception:
        logger.exception("Failed to decode serialized run result")
        return False

    try:
        doc_ref.set(
            {
                "result": inline_obj,
                "result_gcs_uri": None,
                "updated_at": time.time(),
            },
            merge=True,
        )
        return True
    except Exception:
        logger.exception("Failed to store run result inline in Firestore")
        return False

def _upload_run_result(run_id: str, result: Any) -> Optional[str]:
    client = _get_storage_client()
    if client is None:
        return None

    try:
        bucket_name, prefix = _get_runs_gcs_bucket_prefix()
    except Exception:
        logger.exception("Failed to read RUNS_GCS_URI")
        return None

    object_name = "/".join([p for p in [prefix, run_id, "result.json"] if p])
    payload = _serialize_result_json_bytes(result)
    if payload is None:
        return None

    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        blob.upload_from_string(payload, content_type="application/json")
        return f"gs://{bucket_name}/{object_name}"
    except Exception:
        logger.exception("Failed to upload run result to GCS")
        return None

def _download_json_from_gcs(uri: str) -> Any:
    client = _get_storage_client()
    if client is None:
        return None

    try:
        bucket_name, object_name = _parse_gcs_uri(uri)
    except Exception:
        logger.exception("Invalid GCS URI")
        return None

    try:
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_name)
        data = blob.download_as_bytes()
        return json.loads(data.decode("utf-8"))
    except Exception:
        logger.exception("Failed to download run result from GCS")
        return None

def create_run(run_id: str) -> None:
    if not _use_durable_store():
        with _LOCK:
            _RUNS[run_id] = RunRecord(status="pending", current_node=None, progress_pct=0, metadata={}, artifacts=[])
            _RUN_ARTIFACTS[run_id] = {}
        return

    # A run kept in memory while the durable store is selected would be invisible
    # to get_run, so storage problems stop the run instead of hiding it.
    doc_ref = _firestore_doc_ref(run_id)
    if doc_ref is None:
        raise RunStoreUnavailable("Run storage is unavailable; the run was not started.")

    now = time.time()
    try:
        doc_ref.set(
            {
                "status": "pending",
                "current_node": None,
                "progress_pct": 0,
                "warnings": [],
                "metadata": {},
                "artifacts": [],
                "error": None,
                "traceback": None,
                "result_gcs_uri": None,
                "created_at": now,
                "updated_at": now,
            },
            merge=True,
        )
    except Exception as exc:
        logger.exception("Failed to create run %s in Firestore", run_id)
        raise RunStoreUnavailable("Run storage is unavailable; the run was not started.") from exc

def get_run(run_id: str) -> Optional[RunRecord]:
    if not _use_durable_store():
        with _LOCK:
            rec = _RUNS.get(run_id)
            if rec is None:
                return None
            return RunRecord(
                status=rec.status,
                current_node=rec.current_node,
                progress_pct=rec.progress_pct,
                warnings=list(rec.warnings),
                metadata=dict(rec.metadata),
                artifacts=[RunArtifactDescriptor(**asdict(item)) for item in rec.artifacts],
                error=rec.error,
                traceback=rec.traceback,
                result=rec.result,
                created_at=rec.created_at,
                updated_at=rec.updated_at,
            )

    doc_ref = _firestore_doc_ref(run_id)
    if doc_ref is None:
        return None

    try:
        snap = doc_ref.get()
    except Exception:
        logger.exception("Failed to read run from Firestore")
        return None

    if not getattr(snap, "exists", False):
        return None

    data = snap.to_dict() or {}
    status: RunStatus = data.get("status") or "pending"
    current_node = data.get("current_node")
    progress_pct = int(data.get("progress_pct") or 0)
    warnings = list(data.get("warnings") or [])
    metadata = dict(data.get("metadata") or {})
    artifacts = [
        _artifact_descriptor_from_dict(item)
        for item in list(data.get("artifacts") or [])
        if isinstance(item, dict)
    ]
    error = data.get("error")
    tb = data.get("traceback")
    created_at = float(data.get("created_at") or time.time())
    updated_at = float(data.get("updated_at") or created_at)

    result: Any = None
    if status == "completed":
        uri = data.get("result_gcs_uri")
        if isinstance(uri, str) and uri.strip():
            result = _download_json_from_gcs(uri)
        else:
            result = data.get("result")

    return RunRecord(
        status=status,
        current_node=current_node,
        progress_pct=progress_pct,
        warnings=warnings,
        metadata=metadata,
        artifacts=artifacts,
        error=error,
        traceback=tb,
        result=result,
        created_at=created_at,
        updated_at=updated_at,
    )

def add_run_artifact(
    run_id: str,
    *,
    artifact_id: Optional[str] = None,
    label: str,
    mime_type: str,
    file_name: str,
    download_path: str,
    content: Any,
) -> Dict[str, Any]:
    artifact_id = str(artifact_id or f"artifact_{uuid.uuid4().hex[:12]}")
    payload = _normalize_artifact_content(content)
    descriptor = RunArtifactDescriptor(
        artifact_id=artifact_id,
        label=label,
        mime_type=mime_type,
        file_name=file_name,
        download_path=download_path,
    )

    if not _use_durable_store():
        with _LOCK:
            rec = _RUNS.get(run_id)
            if rec is None:
                raise KeyError(f"Run ID not found: {run_id}")
            rec.artifacts = [item for item in rec.artifacts if item.artifact_id != artifact_id]
            rec.artifacts.append(descriptor)
            _RUN_ARTIFACTS.setdefault(run_id, {})[artifact_id] = RunArtifact(
                artifact_id=artifact_id,
                label=label,
                mime_type=mime_type,
                file_name=file_name,
                download_path=download_path,
                content=payload,
            )
            rec.updated_at = time.time()
        return asdict(descriptor)

    doc_ref = _firestore_doc_ref(run_id)
    if doc_ref is None:
        raise KeyError(f"Run ID not found: {run_id}")

    try:
        snap = doc_ref.get()
    except Exception as exc:
        logger.exception("Failed to read run before adding artifact")
        raise KeyError(f"Run ID not found: {run_id}") from exc

    if not getattr(snap, "exists", False):
        raise KeyError(f"Run ID not found: {run_id}")

    existing = snap.to_dict() or {}
    artifact_entry = asdict(descriptor)
    storage_uri = _upload_run_artifact(run_id, artifact_id, file_name, payload, mime_type=mime_type)
    if storage_uri:
        artifact_entry["storage_uri"] = storage_uri
    elif len(payload) <= _INLINE_ARTIFACT_MAX_BYTES:
        artifact_entry["inline_content_b64"] = base64.b64encode(payload).decode("ascii")
    else:
        raise RuntimeError("Artifact persistence failed: payload too large for inline fallback.")

    current_artifacts = list(existing.get("artifacts") or [])
    current_artifacts = [
        item for item in current_artifacts
        if not isinstance(item, dict) or str(item.get("artifact_id")) != artifact_id
    ]
    current_artifacts.append(artifact_entry)

    try:
        doc_ref.set({"artifacts": current_artifacts, "updated_at": time.time()}, merge=True)
    except Exception as exc:
        logger.exception("Failed to persist run artifact metadata")
        raise RuntimeError("Artifact persistence failed.") from exc

    return asdict(descriptor)

def get_run_artifact(run_id: str, artifact_id: str) -> Optional[RunArtifact]:
    if not _use_durable_store():
        with _LOCK:
            artifact = (_RUN_ARTIFACTS.get(run_id) or {}).get(artifact_id)
            if artifact is None:
                return None
            return RunArtifact(
                artifact_id=artifact.artifact_id,
                label=artifact.label,
                mime_type=artifact.mime_type,
                file_name=artifact.file_name,
                download_path=artifact.download_path,
                content=bytes(artifact.content),
            )

    doc_ref = _firestore_doc_ref(run_id)
    if doc_ref is None:
        return None

    try:
        snap = doc_ref.get()
    except Exception:
        logger.exception("Failed to read run artifact metadata from Firestore")
        return None

    if not getattr(snap, "exists", False):
        return None

    data = snap.to_dict() or {}
    for item in list(data.get("artifacts") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("artifact_id") or "") != artifact_id:
            continue

        payload = b""
        storage_uri = item.get("storage_uri")
        inline_content_b64 = item.get("inline_content_b64")
        if isinstance(storage_uri, str) and storage_uri.strip():
            downloaded = _download_artifact_bytes(storage_uri)
            if downloaded is None:
                return None
            payload = downloaded
        elif isinstance(inline_content_b64, str) and inline_content_b64:
            try:
                payload = base64.b64decode(inline_content_b64)
            except Exception:
                logger.exception("Failed to decode inline artifact payload")
                return None
        else:
            return None

        public_item = _public_artifact_dict(item)
        return RunArtifact(
            artifact_id=public_item["artifact_id"],
            label=public_item["label"],
            mime_type=public_item["mime_type"],
            file_name=public_item["file_name"],
            download_path=public_item["download_path"],
            content=payload,
            storage_uri=storage_uri if isinstance(storage_uri, str) else None,
            inline_content_b64=inline_content_b64 if isinstance(inline_content_b64, str) else None,
        )

    return None

def update_run(
    run_id: str,
    *,
    status: Any = _UNSET,
    current_node: Any = _UNSET,
    progress_pct: Any = _UNSET,
    warnings: Any = _UNSET,
    metadata: Any = _UNSET,
    error: Any = _UNSET,
    traceback: Any = _UNSET,
    result: Any = _UNSET,
) -> None:
    if not _use_durable_store():
        with _LOCK:
            rec = _RUNS.get(run_id)
            if rec is None:
                return

            if status is not _UNSET:
                rec.status = status

            if current_node is not _UNSET:
                rec.current_node = current_node

            if progress_pct is not _UNSET:
                rec.progress_pct = max(rec.progress_pct, int(progress_pct))

            if warnings is not _UNSET:
                if warnings:
                    rec.warnings.extend([w for w in warnings if w])
                else:
                    rec.warnings = []

            if metadata is not _UNSET:
                if metadata is None:
                    rec.metadata = {}
                elif isinstance(metadata, dict):
                    rec.metadata.update(metadata)
                else:
                    rec.metadata = {"value": metadata}

            if error is not _UNSET:
                rec.error = error

            if traceback is not _UNSET:
                rec.traceback = traceback

            if result is not _UNSET:
                rec.result = result

            rec.updated_at = time.time()
        return

    doc_ref = _firestore_doc_ref(run_id)
    if doc_ref is None:
        return

    try:
        snap = doc_ref.get()
    except Exception:
        logger.exception("Failed to read run before update")
        return

    if not getattr(snap, "exists", False):
        return

    existing = snap.to_dict() or {}
    updates: Dict[str, Any] = {}

    if status is not _UNSET:
        updates["status"] = status

    if current_node is not _UNSET:
        updates["current_node"] = current_node

    if progress_pct is not _UNSET:
        try:
            existing_progress = int(existing.get("progress_pct") or 0)
        except Exception:
            existing_progress = 0
        updates["progress_pct"] = max(existing_progress, int(progress_pct))

    if warnings is not _UNSET:
        if warnings:
            current_warnings = list(existing.get("warnings") or [])
            current_warnings.extend([w for w in warnings if w])
            updates["warnings"] = current_warnings
        else:
            updates["warnings"] = []

    if metadata is not _UNSET:
        if metadata is None:
            updates["metadata"] = {}
        elif isinstance(metadata, dict):
            current_meta = dict(existing.get("metadata") or {})
            current_meta.update(metadata)
            updates["metadata"] = current_meta
        else:
            updates["metadata"] = {"value": metadata}

    if error is not _UNSET:
        updates["error"] = error

    if traceback is not _UNSET:
        updates["traceback"] = traceback

    if result is not _UNSET:
        uri = _upload_run_result(run_id, result)
        if uri:
            updates["result_gcs_uri"] = uri
            updates["result"] = None
        else:
            payload = _serialize_result_json_bytes(result)
            if payload is not None and len(payload) <= _INLINE_RESULT_MAX_BYTES:
                try:
                    updates["result"] = json.loads(payload.decode("utf-8"))
                    updates["result_gcs_uri"] = None
                except Exception:
                    logger.exception("Failed to store inline result during update")

    updates["updated_at"] = time.time()

    try:
        doc_ref.set(updates, merge=True)
    except Exception:
        logger.exception("Failed to update run in Firestore")

def update_run_progress(run_id: str, *, current_node: str, progress_pct: int) -> None:
    update_run(run_id, current_node=current_node, progress_pct=progress_pct)

def set_run_completed(run_id: str, *, result: Any) -> None:
    if not _use_durable_store():
        update_run(
            run_id,
            status="completed",
            current_node="END",
            progress_pct=100,
            result=result,
            error=None,
            traceback=None,
        )
        return

    uri = _upload_run_result(run_id, result)

    if uri:
        update_run(
            run_id,
            status="completed",
            current_node="END",
            progress_pct=100,
            error=None,
            traceback=None,
            result=_UNSET,
            metadata=_UNSET,
            warnings=_UNSET,
        )
        doc_ref = _firestore_doc_ref(run_id)
        if doc_ref is None:
            return
        try:
            doc_ref.set({"result_gcs_uri": uri, "result": None, "updated_at": time.time()}, merge=True)
        except Exception:
            logger.exception("Failed to set result_gcs_uri")
        return

    doc_ref = _firestore_doc_ref(run_id)
    if doc_ref is not None:
        inline_ok = _try_store_result_inline(doc_ref, run_id, result)
        if inline_ok:
            update_run(
                run_id,
                status="completed",
                current_node="END",
                progress_pct=100,
                error=None,
                traceback=None,
                result=_UNSET,
                metadata=_UNSET,
                warnings=_UNSET,
            )
            return

    set_run_failed(
        run_id,
        error="Result persistence failed (GCS upload failed and inline Firestore fallback was unavailable/too large)",
        traceback=None,
        current_node="END",
    )

def set_run_failed(
    run_id: str,
    *,
    error: str,
    traceback: Optional[str] = None,
    current_node: Optional[str] = None,
) -> None:
    update_run(
        run_id,
        status="failed",
        current_node=current_node,
        progress_pct=0,
        error=error,
        traceback=traceback,
    )
