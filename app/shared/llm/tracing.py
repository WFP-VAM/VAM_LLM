"""One record per model call, for every drafter.

Public diagnostics never contain prompt or reply text. Full payload capture is opt-in and goes to a private
GCS prefix, never to a downloadable run artifact. Every call is also logged as JSON lines on the
`app.llm_trace` logger.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Literal, Mapping, Optional, Protocol

from pydantic import BaseModel, Field

from app.shared.cloud import parse_gcs_uri, storage_client
from app.shared.util import redact_secrets

from .errors import REQUEST_PERSISTENCE, RESPONSE_PERSISTENCE
from .profiles import ModelProfile
from .protocol import FilePart, LLMRequest, LLMResponse

TRACE_SCHEMA_VERSION = "2.0"

TransportStatus = Literal["not_started", "succeeded", "failed"]
ProcessingStatus = Literal["not_requested", "passed", "failed"]
CallStatus = Literal["started", "succeeded", "recovered", "failed"]
PayloadStatus = Literal["disabled", "pending", "stored", "failed"]

# Failures of the reply itself, as opposed to reaching the model or storing the audit trail.
CONTRACT_STAGES = {"response_extraction", "json_parse", "contract_validation"}
_PERSISTENCE_STAGES = {"request_persistence", "response_persistence"}
_CALL_FAILURE_STAGES = {"transport", *_PERSISTENCE_STAGES}


class LLMCallDiagnostic(BaseModel):
    """Sanitized record of one attempt at a model call."""

    trace_schema_version: Literal["2.0"] = TRACE_SCHEMA_VERSION
    call_id: str
    sequence: int = Field(ge=1)
    service: str
    run_id: str
    node: str
    operation: str
    artifact_type: Optional[str] = None
    artifact_id: Optional[str] = None
    correction_attempt: int = Field(default=0, ge=0)
    provider: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    model: str
    location: str
    configured_timeout_seconds: float = 60.0
    configured_max_retries: int = 2
    started_at: str
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = Field(default=None, ge=0)
    status: CallStatus = "started"
    transport_status: TransportStatus = "not_started"
    response_extraction_status: ProcessingStatus = "not_requested"
    json_parse_status: ProcessingStatus = "not_requested"
    contract_validation_status: ProcessingStatus = "not_requested"
    json_root_value_count: Optional[int] = Field(default=None, ge=0)
    json_code_fence_count: Optional[int] = Field(default=None, ge=0)
    json_trailing_character_count: Optional[int] = Field(default=None, ge=0)
    json_error_line: Optional[int] = Field(default=None, ge=1)
    json_error_column: Optional[int] = Field(default=None, ge=1)
    json_error_position: Optional[int] = Field(default=None, ge=0)
    prompt_message_count: int = Field(default=0, ge=0)
    prompt_character_count: int = Field(default=0, ge=0)
    prompt_sha256: str = ""
    response_content_shape: Optional[str] = None
    response_character_count: Optional[int] = Field(default=None, ge=0)
    response_sha256: Optional[str] = None
    provider_response_id: Optional[str] = None
    outcome: Optional[str] = None
    contract_schema_hash: Optional[str] = None
    transport_schema_hash: Optional[str] = None
    finish_reason: Optional[str] = None
    token_usage: Dict[str, Optional[int]] = Field(default_factory=dict)
    failure_code: Optional[str] = None
    failure_stage: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    payload_persistence_status: PayloadStatus = "disabled"
    disposition: Optional[str] = None
    # Added with the shared client; absent from records written before it.
    attempt: int = Field(default=1, ge=1)
    work_item: Optional[str] = None
    retry_of: Optional[str] = None
    repair_of: Optional[str] = None
    transient: Optional[bool] = None


class LLMRunDiagnostics(BaseModel):
    """Sanitized run-level summary propagated through APIs and live metadata."""

    trace_schema_version: Literal["2.0"] = TRACE_SCHEMA_VERSION
    service: str
    run_id: str
    status: Literal["not_started", "running", "completed", "failed"] = "not_started"
    current_call_id: Optional[str] = None
    active_call_ids: List[str] = Field(default_factory=list)
    total_calls: int = Field(default=0, ge=0)
    succeeded_calls: int = Field(default=0, ge=0)
    recovered_calls: int = Field(default=0, ge=0)
    failed_calls: int = Field(default=0, ge=0)
    contract_failed_calls: int = Field(default=0, ge=0)
    payload_capture_enabled: bool = False
    payload_storage_configured: bool = False
    payload_persistence_failures: int = Field(default=0, ge=0)
    calls: List[LLMCallDiagnostic] = Field(default_factory=list)


class LLMObservabilityConfig(BaseModel):
    trace_schema_version: Literal["2.0"] = TRACE_SCHEMA_VERSION
    payload_capture_enabled: bool
    payload_storage_configured: bool
    configuration_status: Literal["disabled", "configured", "invalid"]
    retention_days: int = 30


LiveSink = Callable[[Dict[str, Any]], None]


class CallAudit(Protocol):
    """A mandatory store of every request and reply, such as Seasonal's analysis record.

    `requested`, `responded` and `validated` failures fail the call. `failed` is told about a call that failed for
    any other reason; its own errors are logged, and the call's original error stands.
    """

    def requested(self, record: LLMCallDiagnostic, request: LLMRequest) -> None: ...

    def responded(self, record: LLMCallDiagnostic, response: LLMResponse) -> None: ...

    def validated(self, record: LLMCallDiagnostic) -> None: ...

    def failed(self, record: LLMCallDiagnostic) -> None: ...


_SAFE_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")


def _configured_bool(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def observability_config() -> LLMObservabilityConfig:
    enabled = _configured_bool("LLM_TRACE_PAYLOADS")
    uri = (os.getenv("LLM_TRACE_GCS_URI") or "").strip()
    configured = bool(re.match(r"^gs://[^/\s]+(?:/.*)?$", uri))
    status: Literal["disabled", "configured", "invalid"]
    if not enabled:
        status = "disabled"
    elif configured:
        status = "configured"
    else:
        status = "invalid"
    return LLMObservabilityConfig(
        payload_capture_enabled=enabled,
        payload_storage_configured=configured,
        configuration_status=status,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_slug(value: Any) -> str:
    normalized = _SAFE_SLUG_PATTERN.sub("-", str(value or "").strip()).strip("-.")
    return normalized or "unknown"


def _sanitize_error(value: Any) -> str:
    return redact_secrets(str(value or "").strip())[:1000]


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe(model_dump(mode="json"))
        except Exception:
            pass
    return str(value)


def prompt_text(request: LLMRequest) -> str:
    """What prompt size and hash are computed over: system instruction (if any), text parts and file URIs."""
    pieces = [request.system] if request.system else []
    pieces.extend(json.dumps(part.reference, sort_keys=True) if isinstance(part, FilePart) else part for part in request.parts)
    return "\n".join(pieces)


_TRACE_LOGGER = logging.getLogger("app.llm_trace")
if not _TRACE_LOGGER.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _TRACE_LOGGER.addHandler(_handler)
_TRACE_LOGGER.propagate = False
_TRACE_LOGGER.setLevel(logging.INFO)


def _emit_structured(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    payload = {
        "event": event,
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        **{key: _json_safe(value) for key, value in fields.items()},
    }
    _TRACE_LOGGER.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def log_llm_run_summary(diagnostics: Mapping[str, Any]) -> None:
    """Emit the sanitized final trace summary without call payloads."""
    _emit_structured(
        "llm_run_summary",
        level=logging.ERROR if diagnostics.get("failed_calls") else logging.INFO,
        service=diagnostics.get("service"),
        run_id=diagnostics.get("run_id"),
        status=diagnostics.get("status"),
        total_calls=diagnostics.get("total_calls", 0),
        succeeded_calls=diagnostics.get("succeeded_calls", 0),
        recovered_calls=diagnostics.get("recovered_calls", 0),
        failed_calls=diagnostics.get("failed_calls", 0),
        contract_failed_calls=diagnostics.get("contract_failed_calls", 0),
        payload_capture_enabled=diagnostics.get("payload_capture_enabled", False),
        payload_persistence_failures=diagnostics.get("payload_persistence_failures", 0),
    )


def _persist_payload(
    *,
    service: str,
    run_id: str,
    sequence: int,
    call_id: str,
    payload: Dict[str, Any],
) -> str:
    uri = (os.getenv("LLM_TRACE_GCS_URI") or "").strip()
    bucket_name, prefix = parse_gcs_uri(uri, name="LLM_TRACE_GCS_URI")
    object_parts = [
        part
        for part in (
            prefix,
            "llm-traces",
            "v2",
            _safe_slug(service),
            _safe_slug(run_id),
            f"{sequence:04d}-{_safe_slug(call_id)}.json.gz",
        )
        if part
    ]
    object_name = "/".join(object_parts)
    content = gzip.compress(
        json.dumps(_json_safe(payload), ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    blob = storage_client().bucket(bucket_name).blob(object_name)
    blob.metadata = {
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "service": service,
        "run_id": run_id,
        "retention_days": "30",
    }
    blob.upload_from_string(content, content_type="application/gzip")
    return f"gs://{bucket_name}/{object_name}"


class Tracer:
    """The calls of one run: snapshots for APIs and the live view, JSON logs and optional payload capture.

    A drafter whose audit already keeps every request and reply (Seasonal) turns payload capture off for its runs.
    """

    def __init__(
        self,
        *,
        service: str,
        run_id: str,
        initial: Optional[Mapping[str, Any]] = None,
        live: Optional[LiveSink] = None,
        audit: Optional[CallAudit] = None,
        capture_payloads: bool = True,
    ) -> None:
        self.service = service
        self.run_id = run_id
        self.live = live
        self.audit = audit
        self.capture_payloads = capture_payloads
        self._lock = threading.RLock()
        self._calls: List[LLMCallDiagnostic] = []
        self._started: Dict[str, float] = {}
        if initial:
            if initial.get("trace_schema_version") != TRACE_SCHEMA_VERSION:
                raise ValueError("This trace uses an incompatible runtime format")
            parsed = LLMRunDiagnostics.model_validate(initial)
            self._calls = [item.model_copy(deep=True) for item in parsed.calls]

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            calls = [item.model_copy(deep=True) for item in self._calls]
        failed = [item for item in calls if item.status == "failed"]
        recovered = [item for item in calls if item.status == "recovered"]
        active = [item for item in calls if item.status == "started"]
        status: Literal["not_started", "running", "completed", "failed"]
        if failed:
            status = "failed"
        elif active:
            status = "running"
        elif calls:
            status = "completed"
        else:
            status = "not_started"
        config = observability_config()
        return LLMRunDiagnostics(
            service=self.service,
            run_id=self.run_id,
            status=status,
            current_call_id=active[-1].call_id if active else None,
            active_call_ids=[item.call_id for item in active],
            total_calls=len(calls),
            succeeded_calls=sum(item.status in {"succeeded", "recovered"} for item in calls),
            recovered_calls=len(recovered),
            failed_calls=len(failed),
            contract_failed_calls=sum(item.failure_stage in CONTRACT_STAGES for item in failed),
            payload_capture_enabled=self._captures(config),
            payload_storage_configured=config.payload_storage_configured,
            payload_persistence_failures=sum(item.payload_persistence_status == "failed" for item in calls),
            calls=calls,
        ).model_dump(mode="json")

    def _captures(self, config: LLMObservabilityConfig) -> bool:
        return self.capture_payloads and config.payload_capture_enabled

    def mark_recovered(self, call_id: str, *, disposition: str) -> None:
        """A failed attempt whose call later succeeded: successful at run level, still auditable."""
        with self._lock:
            record = next((item for item in self._calls if item.call_id == call_id), None)
            if record is None or record.status != "failed":
                raise ValueError(f"Failed LLM call not found: {call_id}")
            record.status = "recovered"
            record.disposition = disposition
        _emit_structured(
            "llm_call_recovered", service=self.service, run_id=self.run_id, call_id=call_id, disposition=disposition
        )
        self._notify()

    def record_skip(
        self,
        *,
        node: str,
        operation: str,
        reason: str,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
    ) -> None:
        _emit_structured(
            "llm_call_skipped",
            service=self.service,
            run_id=self.run_id,
            node=node,
            operation=operation,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            reason=reason,
        )

    def update(self, record: LLMCallDiagnostic, **fields: Any) -> None:
        with self._lock:
            for key, value in fields.items():
                setattr(record, key, value)

    def start(
        self,
        profile: ModelProfile,
        request: LLMRequest,
        *,
        retry_of: Optional[str] = None,
        transport_schema_hash: Optional[str] = None,
    ) -> LLMCallDiagnostic:
        """Record a new call. A call that retries or repairs an earlier one is that call's next attempt."""
        text = prompt_text(request)
        retry_of = retry_of or request.retry_of
        with self._lock:
            earlier = next(
                (item for item in self._calls if item.call_id in {retry_of, request.repair_of}), None
            )
            attempt = earlier.attempt + 1 if earlier is not None else 1
            sequence = len(self._calls) + 1
            record = LLMCallDiagnostic(
                call_id=f"llm-{sequence:04d}-{uuid.uuid4().hex[:8]}",
                sequence=sequence,
                service=self.service,
                run_id=self.run_id,
                node=request.node,
                operation=request.operation,
                artifact_type=request.artifact_type,
                artifact_id=request.artifact_id,
                correction_attempt=request.correction_attempt,
                provider=profile.provider,
                parameters={**{name: getattr(profile, name) for name in ("temperature", "thinking_level", "media_resolution",
                                                                     "include_thoughts", "candidate_count")},
                            "max_output_tokens": request.max_output_tokens or profile.max_output_tokens},
                contract_schema_hash=_sha256(json.dumps(request.response_schema, ensure_ascii=False, sort_keys=True,
                                                        separators=(",", ":"))) if request.response_schema is not None else None,
                transport_schema_hash=transport_schema_hash,
                model=profile.model,
                location=profile.location,
                configured_timeout_seconds=request.timeout_seconds or profile.timeout_seconds,
                configured_max_retries=max(0, profile.attempts - 1),
                started_at=_utc_now(),
                prompt_message_count=len(request.parts) + (1 if request.system else 0),
                prompt_character_count=len(text),
                prompt_sha256=_sha256(text),
                payload_persistence_status="pending" if self._captures(observability_config()) else "disabled",
                attempt=attempt,
                work_item=request.work_item,
                retry_of=retry_of,
                repair_of=request.repair_of,
            )
            self._calls.append(record)
            self._started[record.call_id] = time.perf_counter()
        _emit_structured(
            "llm_call_started",
            service=self.service,
            run_id=self.run_id,
            call_id=record.call_id,
            sequence=record.sequence,
            node=record.node,
            operation=record.operation,
            artifact_type=record.artifact_type,
            artifact_id=record.artifact_id,
            correction_attempt=record.correction_attempt,
            attempt=attempt,
            model=record.model,
            location=record.location,
            prompt_message_count=record.prompt_message_count,
            prompt_character_count=record.prompt_character_count,
            prompt_sha256=record.prompt_sha256,
        )
        self._notify()
        if self.audit is not None:
            try:
                self.audit.requested(record, request)
            except Exception as exc:
                self.failed(record, code=REQUEST_PERSISTENCE, stage="request_persistence", error=exc, payload={})
                raise
        return record

    def responded(self, record: LLMCallDiagnostic, response: LLMResponse) -> None:
        text = response.text.strip()
        self.update(
            record,
            transport_status="succeeded",
            outcome=response.outcome,
            finish_reason=response.finish_reason,
            token_usage=dict(response.usage),
            provider_response_id=response.response_id,
            response_content_shape="text",
            response_character_count=len(text),
            response_sha256=_sha256(text),
        )
        if self.audit is not None:
            try:
                self.audit.responded(record, response)
            except Exception as exc:
                self.failed(record, code=RESPONSE_PERSISTENCE, stage="response_persistence", error=exc, payload={})
                raise

    def succeeded(self, record: LLMCallDiagnostic, payload: Dict[str, Any]) -> None:
        if self.audit is not None:
            try:
                self.audit.validated(record)
            except Exception as exc:
                self.failed(record, code=RESPONSE_PERSISTENCE, stage="response_persistence", error=exc, payload=payload)
                raise
        self.update(record, status="succeeded", completed_at=_utc_now(), duration_ms=self._elapsed_ms(record))
        self._finish_payload(record, payload)
        _emit_structured("llm_call_succeeded", **record.model_dump(mode="json"))
        self._notify()

    def failed(
        self,
        record: LLMCallDiagnostic,
        *,
        code: str,
        stage: str,
        error: BaseException,
        payload: Dict[str, Any],
        transient: Optional[bool] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        updates: Dict[str, Any] = {
            "status": "failed",
            "failure_code": code,
            "failure_stage": stage,
            "error_type": type(error).__name__,
            "error_message": _sanitize_error(error),
            "completed_at": _utc_now(),
            "duration_ms": self._elapsed_ms(record),
            "transient": transient,
        }
        if stage == "transport":
            updates["transport_status"] = "failed"
        updates.update(fields or {})
        self.update(record, **updates)
        payload["error"] = {
            "type": type(error).__name__,
            "message": _sanitize_error(error),
            "stage": stage,
            "failure_code": code,
        }
        self._finish_payload(record, payload)
        if self.audit is not None and stage not in _PERSISTENCE_STAGES:
            try:
                self.audit.failed(record)
            except Exception as exc:
                _emit_structured(
                    "llm_audit_failed",
                    level=logging.ERROR,
                    service=self.service,
                    run_id=self.run_id,
                    call_id=record.call_id,
                    error_type=type(exc).__name__,
                    error_message=_sanitize_error(exc),
                )
        _emit_structured(
            "llm_call_failed" if stage in _CALL_FAILURE_STAGES else "llm_response_contract_failed",
            level=logging.ERROR,
            **record.model_dump(mode="json"),
        )
        self._notify()

    def _elapsed_ms(self, record: LLMCallDiagnostic) -> Optional[int]:
        with self._lock:
            started = self._started.pop(record.call_id, None)
        if started is None:
            return None
        return max(0, int((time.perf_counter() - started) * 1000))

    def _notify(self) -> None:
        if self.live is None:
            return
        try:
            self.live(self.snapshot())
        except Exception as exc:
            _emit_structured(
                "llm_trace_sink_failed",
                level=logging.ERROR,
                service=self.service,
                run_id=self.run_id,
                error_type=type(exc).__name__,
                error_message=_sanitize_error(exc),
            )
            if getattr(self.live, "requires_persistence", False):
                raise

    def _finish_payload(self, record: LLMCallDiagnostic, payload: Dict[str, Any]) -> None:
        config = observability_config()
        if not self._captures(config):
            self.update(record, payload_persistence_status="disabled")
            return
        if not config.payload_storage_configured:
            self.update(record, payload_persistence_status="failed")
            _emit_structured(
                "llm_payload_persistence_failed",
                level=logging.ERROR,
                service=self.service,
                run_id=self.run_id,
                call_id=record.call_id,
                failure_code="payload_storage_not_configured",
            )
            return
        payload["diagnostic"] = record.model_dump(mode="json")
        try:
            uri = _persist_payload(
                service=self.service,
                run_id=self.run_id,
                sequence=record.sequence,
                call_id=record.call_id,
                payload=payload,
            )
            self.update(record, payload_persistence_status="stored")
            _emit_structured(
                "llm_payload_persisted",
                service=self.service,
                run_id=self.run_id,
                call_id=record.call_id,
                payload_uri=uri,
            )
        except Exception as exc:
            self.update(record, payload_persistence_status="failed")
            _emit_structured(
                "llm_payload_persistence_failed",
                level=logging.ERROR,
                service=self.service,
                run_id=self.run_id,
                call_id=record.call_id,
                failure_code="payload_persistence_failed",
                error_type=type(exc).__name__,
                error_message=_sanitize_error(exc),
            )


_CURRENT: ContextVar[Optional[Tracer]] = ContextVar("llm_tracer", default=None)


def current_tracer(*, service: str, run_id: str, initial: Optional[Mapping[str, Any]] = None) -> Tracer:
    """The tracer of the run in progress, or a new one continuing earlier diagnostics (for a node run directly)."""
    current = _CURRENT.get()
    if current is not None and current.service == service and current.run_id == run_id:
        return current
    return Tracer(service=service, run_id=run_id, initial=initial)


@contextmanager
def tracing_run(
    *,
    service: str,
    run_id: str,
    initial: Optional[Mapping[str, Any]] = None,
    live: Optional[LiveSink] = None,
    audit: Optional[CallAudit] = None,
) -> Iterator[Tracer]:
    """Make one tracer the current one for a run, so every node of it records into the same trace."""
    tracer = Tracer(service=service, run_id=run_id, initial=initial, live=live, audit=audit)
    token = _CURRENT.set(tracer)
    try:
        yield tracer
    finally:
        _CURRENT.reset(token)
