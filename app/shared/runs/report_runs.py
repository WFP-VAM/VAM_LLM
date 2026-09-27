"""Runs of the report drafters (Market Monitor and MFI): one background job each, from pending to an end state.

A run is one record in the app's store: Firestore and GCS when `RUNS_BACKEND` or `RUNS_GCS_URI` configures
durable storage, otherwise this process's memory. Every change is a transaction.

The run's own work may write only while the run is pending or running and within its deadline, and each
accepted write moves the deadline on. A run whose work falls silent for longer than its drafter allows, for
example because the server restarted, reads as interrupted, and its work can no longer write to it.

The result and the artifacts are immutable objects, read only when asked for. Records written by the
previous run store (`async_runs`) stay readable.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Literal, Optional

from app.shared.cloud import parse_gcs_uri, read_gcs_uri

from .executor import Superseded, expire_overdue, fenced
from .store import CloudStore, MemoryStore, Missing

RunStatus = Literal["pending", "running", "completed", "failed", "interrupted"]

ACTIVE = ("pending", "running")
RECORD_VERSION = 2
# How long a run's work may go without writing to it before the run reads as interrupted.
DEFAULT_SILENCE_SECONDS = 1800
INTERRUPTED = "The report stopped before it finished, for example because the server restarted. Generate it again."

logger = logging.getLogger(__name__)
clock = time.time
_UNSET = object()


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


class RunRecord:
    """A run as its readers see it. `result` is read from storage on first use, so status polls never load it."""

    def __init__(
        self,
        *,
        status: RunStatus = "pending",
        current_node: Optional[str] = None,
        progress_pct: int = 0,
        warnings: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        artifacts: Optional[List[RunArtifactDescriptor]] = None,
        error: Optional[str] = None,
        traceback: Optional[str] = None,
        created_at: float = 0.0,
        updated_at: float = 0.0,
        service: Optional[str] = None,
        result_loader: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.status = status
        self.current_node = current_node
        self.progress_pct = progress_pct
        self.warnings = list(warnings or [])
        self.metadata = dict(metadata or {})
        self.artifacts = list(artifacts or [])
        self.error = error
        self.traceback = traceback
        self.created_at = created_at
        self.updated_at = updated_at
        self.service = service
        self._result: Any = None
        self._result_loader = result_loader

    @property
    def result(self) -> Any:
        if self._result_loader is not None:
            loader, self._result_loader = self._result_loader, None
            self._result = loader()
        return self._result


class RunStoreUnavailable(RuntimeError):
    """Durable run storage is configured but cannot be written, so the run is not started."""


_STORE: Any = None
_STORE_LOCK = threading.Lock()


def _open_store() -> Any:
    backend = (os.getenv("RUNS_BACKEND") or "").strip().lower()
    uri = (os.getenv("RUNS_GCS_URI") or "").strip()
    durable = backend in {"firestore_gcs", "firestore", "gcs"} or (backend == "" and bool(uri))
    if not durable:
        return MemoryStore("runs", missing="Run not found")
    bucket, prefix = parse_gcs_uri(uri, name="RUNS_GCS_URI")
    return CloudStore(
        project=None,
        database=(os.getenv("RUNS_FIRESTORE_DATABASE") or "").strip() or "vam-llm-async",
        collection=(os.getenv("RUNS_FIRESTORE_COLLECTION") or "").strip() or "async_runs",
        bucket=bucket,
        prefix=prefix or "runs",
        capacity="The run record is too large to store",
        missing="Run not found",
    )


def _store() -> Any:
    """The run store this process uses. A store that cannot open is not kept, so the next call tries again."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = _open_store()
        return _STORE


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (set, tuple)):
        return list(obj)
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return str(obj)
    iso = getattr(obj, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    item = getattr(obj, "item", None)  # numpy scalars
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return str(obj)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, default=_json_default).encode("utf-8")


def _plain(value: Any) -> Any:
    """`value` as stored: plain JSON types, whichever store is used."""
    return json.loads(_json_bytes(value))


def _deadline(record: Dict[str, Any]) -> float:
    if record.get("deadline"):
        return float(record["deadline"])
    # A record of the previous store: its last write is the last sign of life.
    return float(record.get("updated_at") or record.get("created_at") or 0) + DEFAULT_SILENCE_SECONDS


def _overdue(record: Optional[Dict[str, Any]]) -> bool:
    return bool(record) and record.get("status") in ACTIVE and _deadline(record) <= clock()


def _interrupt(record: Dict[str, Any]) -> None:
    record.update(status="interrupted", error=INTERRUPTED, traceback=None)


def _work_write(run_id: str, change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    """A write by the run's own work: refused once the run has ended or is overdue; accepted, it moves the deadline."""
    def apply(current: Dict[str, Any]) -> None:
        change(current)
        current["deadline"] = clock() + int(current.get("silence_seconds") or DEFAULT_SILENCE_SECONDS)

    return fenced(
        _store(),
        run_id,
        apply,
        holds=lambda current: current.get("status") in ACTIVE and not _overdue(current),
        refused=f"Run {run_id} has already ended; its work can no longer write to it",
        clock=clock,
    )


def _apply(
    record: Dict[str, Any],
    *,
    status: Any = _UNSET,
    current_node: Any = _UNSET,
    progress_pct: Any = _UNSET,
    warnings: Any = _UNSET,
    metadata: Any = _UNSET,
    live_outputs: Any = _UNSET,
    error: Any = _UNSET,
    traceback: Any = _UNSET,
) -> None:
    if status is not _UNSET:
        record["status"] = status
    if current_node is not _UNSET:
        record["current_node"] = current_node
    if progress_pct is not _UNSET:
        record["progress_pct"] = max(int(record.get("progress_pct") or 0), int(progress_pct))
    if warnings is not _UNSET:
        record["warnings"] = [*(record.get("warnings") or []), *_plain([w for w in warnings if w])] if warnings else []
    if metadata is not _UNSET:
        if metadata is None:
            record["metadata"] = {}
        elif isinstance(metadata, dict):
            record["metadata"] = {**(record.get("metadata") or {}), **_plain(metadata)}
        else:
            record["metadata"] = {"value": _plain(metadata)}
    if live_outputs is not _UNSET and live_outputs:
        current = dict(record.get("metadata") or {})
        current["live_outputs"] = {**(current.get("live_outputs") or {}), **_plain(live_outputs)}
        record["metadata"] = current
    if error is not _UNSET:
        record["error"] = error
    if traceback is not _UNSET:
        record["traceback"] = traceback


def create_run(run_id: str, *, service: str, silence_seconds: int = DEFAULT_SILENCE_SECONDS) -> None:
    """Record a new pending run. If durable storage cannot take it, raise RunStoreUnavailable: the run does not start."""
    now = clock()
    record = {
        "id": run_id,
        "service": service,
        "record_version": RECORD_VERSION,
        "revision": 1,
        "status": "pending",
        "current_node": None,
        "progress_pct": 0,
        "warnings": [],
        "metadata": {},
        "artifacts": [],
        "error": None,
        "traceback": None,
        "result_object": None,
        "created_at": now,
        "updated_at": now,
        "silence_seconds": silence_seconds,
        "deadline": now + silence_seconds,
    }
    try:
        _store().mutate(run_id, lambda _current: record)
    except Exception as exc:
        logger.exception("Run %s could not be created", run_id)
        raise RunStoreUnavailable("Run storage is unavailable; the run was not started.") from exc


def get_run(run_id: str) -> Optional[RunRecord]:
    """The run, or None if it does not exist or cannot be read. Overdue work reads as interrupted."""
    try:
        store = _store()
        record = store.get(run_id)
    except Missing:
        return None
    except Exception:
        logger.exception("Run %s could not be read", run_id)
        return None
    try:
        record = expire_overdue(store, run_id, record, overdue=_overdue, interrupt=_interrupt, clock=clock)
    except Exception:
        logger.exception("Run %s could not be marked interrupted", run_id)
    status = record.get("status") or "pending"
    return RunRecord(
        status=status,
        current_node=record.get("current_node"),
        progress_pct=int(record.get("progress_pct") or 0),
        warnings=record.get("warnings"),
        metadata=record.get("metadata"),
        artifacts=[_descriptor(item) for item in record.get("artifacts") or [] if isinstance(item, dict)],
        error=record.get("error"),
        traceback=record.get("traceback"),
        created_at=float(record.get("created_at") or 0),
        updated_at=float(record.get("updated_at") or record.get("created_at") or 0),
        service=record.get("service"),
        result_loader=(lambda: _read_result(store, record)) if status == "completed" else None,
    )


def _read_result(store: Any, record: Dict[str, Any]) -> Any:
    if record.get("result_object"):
        return store.json(record["result_object"])
    uri = record.get("result_gcs_uri")  # written by the previous store
    if isinstance(uri, str) and uri.strip():
        return json.loads(read_gcs_uri(uri))
    return record.get("result")


def update_run(
    run_id: str,
    *,
    status: Any = _UNSET,
    current_node: Any = _UNSET,
    progress_pct: Any = _UNSET,
    warnings: Any = _UNSET,
    metadata: Any = _UNSET,
    live_outputs: Any = _UNSET,
    error: Any = _UNSET,
    traceback: Any = _UNSET,
) -> None:
    """Progress from the run's work. `metadata` merges key by key and `live_outputs` section by section.

    A progress write that cannot be stored is logged and the work goes on, as it always has.
    """
    fields = dict(status=status, current_node=current_node, progress_pct=progress_pct, warnings=warnings,
                  metadata=metadata, live_outputs=live_outputs, error=error, traceback=traceback)
    try:
        _work_write(run_id, lambda record: _apply(record, **fields))
    except Superseded as exc:
        logger.info("%s", exc)
    except Exception:
        logger.exception("Run %s could not be updated", run_id)


def update_run_progress(run_id: str, *, current_node: str, progress_pct: int) -> None:
    update_run(run_id, current_node=current_node, progress_pct=progress_pct)


def set_run_completed(run_id: str, *, result: Any, warnings: Any = _UNSET, metadata: Any = _UNSET) -> None:
    """Store the result and complete the run in one write. Raises if either fails, so the caller can fail the run."""
    store = _store()
    ref = store.put(run_id, _json_bytes(result), "application/json")

    def complete(record: Dict[str, Any]) -> None:
        _apply(record, warnings=warnings, metadata=metadata, progress_pct=100)
        record.update(status="completed", current_node="END", error=None, traceback=None, result_object=ref)

    _work_write(run_id, complete)


def set_run_failed(
    run_id: str,
    *,
    error: str,
    traceback: Optional[str] = None,
    current_node: Optional[str] = None,
) -> None:
    """Fail the run. A run that has already ended keeps its end state."""
    try:
        _work_write(run_id, lambda record: record.update(status="failed", current_node=current_node,
                                                          error=error, traceback=traceback))
    except Superseded as exc:
        logger.info("%s", exc)
    except Exception:
        logger.exception("Run %s could not be marked failed", run_id)


def _descriptor(item: Dict[str, Any]) -> RunArtifactDescriptor:
    return RunArtifactDescriptor(
        artifact_id=str(item.get("artifact_id") or ""),
        label=str(item.get("label") or ""),
        mime_type=str(item.get("mime_type") or "application/octet-stream"),
        file_name=str(item.get("file_name") or "download.bin"),
        download_path=str(item.get("download_path") or ""),
    )


def _content_bytes(content: Any) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode("utf-8")
    return _json_bytes(content)


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
    """Store a downloadable file of the running run and list it in the record; an artifact id replaces its namesake."""
    artifact_id = str(artifact_id or f"artifact_{uuid.uuid4().hex[:12]}")
    descriptor = RunArtifactDescriptor(artifact_id=artifact_id, label=label, mime_type=mime_type,
                                       file_name=file_name, download_path=download_path)
    ref = _store().put(run_id, _content_bytes(content), mime_type)

    def add(record: Dict[str, Any]) -> None:
        kept = [item for item in record.get("artifacts") or []
                if not isinstance(item, dict) or str(item.get("artifact_id")) != artifact_id]
        record["artifacts"] = [*kept, {**asdict(descriptor), "object": ref}]

    try:
        _work_write(run_id, add)
    except Superseded as exc:
        raise KeyError(f"Run ID not found or already ended: {run_id}") from exc
    return asdict(descriptor)


def get_run_artifact(run_id: str, artifact_id: str) -> Optional[RunArtifact]:
    try:
        store = _store()
        record = store.get(run_id)
    except Missing:
        return None
    except Exception:
        logger.exception("Run %s could not be read", run_id)
        return None
    item = next((item for item in record.get("artifacts") or []
                 if isinstance(item, dict) and str(item.get("artifact_id") or "") == artifact_id), None)
    if item is None:
        return None
    storage_uri, inline = item.get("storage_uri"), item.get("inline_content_b64")
    try:
        if item.get("object"):
            content = store.read(item["object"])
        elif isinstance(storage_uri, str) and storage_uri.strip():  # written by the previous store
            content = read_gcs_uri(storage_uri)
        elif isinstance(inline, str) and inline:
            content = base64.b64decode(inline)
        else:
            return None
    except Exception:
        logger.exception("Artifact %s of run %s could not be read", artifact_id, run_id)
        return None
    return RunArtifact(**asdict(_descriptor(item)), content=content,
                       storage_uri=storage_uri if isinstance(storage_uri, str) else None,
                       inline_content_b64=inline if isinstance(inline, str) else None)
