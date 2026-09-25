"""Shared LLM call tracing for report-generation services.

The public diagnostics emitted by this module never contain prompt or response
bodies.  Full payload capture is opt-in and is written directly to a private GCS
prefix without registering a downloadable run artifact.
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
from typing import Any, Callable, Dict, Generic, Iterator, List, Literal, Mapping, Optional, Sequence, TypeVar

from pydantic import BaseModel, Field

from app.shared.llm import llm_runtime_config


TRACE_SCHEMA_VERSION = "1.0"

TransportStatus = Literal["not_started", "succeeded", "failed"]
ProcessingStatus = Literal["not_requested", "passed", "failed"]
CallStatus = Literal["started", "succeeded", "recovered", "failed"]
PayloadStatus = Literal["disabled", "pending", "stored", "failed"]


class LLMCallDiagnostic(BaseModel):
    """Sanitized diagnostic record for one application-level LLM call."""

    trace_schema_version: Literal["1.0"] = TRACE_SCHEMA_VERSION
    call_id: str
    sequence: int = Field(ge=1)
    service: str
    run_id: str
    node: str
    operation: str
    artifact_type: Optional[str] = None
    artifact_id: Optional[str] = None
    correction_attempt: int = Field(default=0, ge=0)
    provider: str = "vertex_ai"
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
    finish_reason: Optional[str] = None
    token_usage: Dict[str, Optional[int]] = Field(default_factory=dict)
    failure_code: Optional[str] = None
    failure_stage: Optional[str] = None
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    payload_persistence_status: PayloadStatus = "disabled"
    disposition: Optional[str] = None


class LLMRunDiagnostics(BaseModel):
    """Sanitized run-level summary propagated through APIs and live metadata."""

    trace_schema_version: Literal["1.0"] = TRACE_SCHEMA_VERSION
    service: str
    run_id: str
    status: Literal["not_started", "running", "completed", "failed"] = "not_started"
    current_call_id: Optional[str] = None
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
    trace_schema_version: Literal["1.0"] = TRACE_SCHEMA_VERSION
    payload_capture_enabled: bool
    payload_storage_configured: bool
    configuration_status: Literal["disabled", "configured", "invalid"]
    retention_days: int = 30


class LLMCallError(RuntimeError):
    """Stable exception raised for transport or response-contract failures."""

    def __init__(
        self,
        *,
        failure_code: str,
        call_id: str,
        node: str,
        operation: str,
        stage: str,
        raw_text: Optional[str] = None,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        task_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> None:
        self.failure_code = failure_code
        self.call_id = call_id
        self.node = node
        self.operation = operation
        self.stage = stage
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.task_id = task_id
        self.batch_id = batch_id
        # Kept in memory only so an operation-specific recovery boundary can
        # normalize formatting. It is deliberately omitted from public metadata,
        # logs, exception messages, and ``to_public_dict``.
        self.raw_text = raw_text
        super().__init__(
            f"LLM call failed [{failure_code}] at {node}/{operation} "
            f"(call_id={call_id})"
        )

    def to_public_dict(self) -> Dict[str, str]:
        return {
            "code": "llm_call_failed",
            "failure_code": self.failure_code,
            "call_id": self.call_id,
            "node": self.node,
            "operation": self.operation,
            "stage": self.stage,
            **({"artifact_type": self.artifact_type} if self.artifact_type else {}),
            **({"artifact_id": self.artifact_id} if self.artifact_id else {}),
            **({"task_id": self.task_id} if self.task_id else {}),
            **({"batch_id": self.batch_id} if self.batch_id else {}),
        }


T = TypeVar("T")


class TracedLLMResult(BaseModel, Generic[T]):
    call_id: str
    raw_text: str
    payload: Any = None
    value: Any = None


TraceSink = Callable[[Dict[str, Any]], None]


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|api[_-]?secret|client[_-]?secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_SAFE_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")
_CURRENT_SESSION: ContextVar[Optional["LLMTraceSession"]] = ContextVar(
    "llm_trace_session", default=None
)


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
    text = str(value or "").strip()
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    return text[:1000]


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


def _message_content(message: Any) -> Any:
    if isinstance(message, Mapping):
        return message.get("content")
    return getattr(message, "content", message)


def _message_role(message: Any) -> str:
    if isinstance(message, Mapping):
        return str(message.get("role") or message.get("type") or "message")
    return str(getattr(message, "type", None) or message.__class__.__name__)


def serialize_messages(messages: Sequence[Any]) -> List[Dict[str, Any]]:
    return [
        {"role": _message_role(message), "content": _json_safe(_message_content(message))}
        for message in messages
    ]


def extract_response_text(response: Any) -> tuple[str, str, Any]:
    """Return normalized text, content-shape label, and JSON-safe raw content."""
    content = getattr(response, "content", response)
    raw_content = _json_safe(content)
    if isinstance(content, str):
        text = content.strip()
        if not text:
            raise ValueError("LLM response content is empty")
        return text, "string", raw_content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                if block.strip():
                    parts.append(block.strip())
                continue
            if not isinstance(block, Mapping):
                continue
            block_type = str(block.get("type") or "").lower()
            text_value = block.get("text")
            if isinstance(text_value, str) and (
                block_type in {"", "text", "output_text"} or text_value.strip()
            ):
                if text_value.strip():
                    parts.append(text_value.strip())
        text = "\n".join(parts).strip()
        if not text:
            raise ValueError("LLM response contains no text blocks")
        return text, "content_blocks", raw_content
    raise TypeError(f"Unsupported LLM response content type: {type(content).__name__}")


def parse_json_object(raw_text: str) -> Dict[str, Any]:
    """Extract the first complete-looking JSON object using existing semantics."""
    cleaned = re.sub(r"```json\s*", "", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```", "", cleaned).strip()
    start_index = cleaned.find("{")
    end_index = cleaned.rfind("}")
    if start_index == -1 or end_index == -1 or end_index < start_index:
        raise ValueError("LLM response does not contain a JSON object")
    parsed = json.loads(cleaned[start_index : end_index + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON root must be an object")
    return parsed


def inspect_json_structure(
    raw_text: str,
    error: Optional[Exception] = None,
) -> Dict[str, Optional[int]]:
    """Describe JSON shape without retaining or emitting response content."""
    cleaned = re.sub(r"```json\s*", "", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```", "", cleaned).strip()
    start_index = cleaned.find("{")
    candidate = cleaned[start_index:] if start_index >= 0 else cleaned
    decoder = json.JSONDecoder()
    cursor = 0
    roots = 0
    first_end: Optional[int] = None
    while cursor < len(candidate):
        while cursor < len(candidate) and candidate[cursor].isspace():
            cursor += 1
        if cursor >= len(candidate):
            break
        try:
            _value, end = decoder.raw_decode(candidate, cursor)
        except json.JSONDecodeError:
            break
        roots += 1
        if first_end is None:
            first_end = end
        cursor = end

    decode_error = error if isinstance(error, json.JSONDecodeError) else None
    return {
        "json_root_value_count": roots,
        "json_code_fence_count": raw_text.count("```"),
        "json_trailing_character_count": (
            len(candidate[first_end:].strip()) if first_end is not None else None
        ),
        "json_error_line": getattr(decode_error, "lineno", None),
        "json_error_column": getattr(decode_error, "colno", None),
        "json_error_position": getattr(decode_error, "pos", None),
    }


def _token_usage(response: Any) -> Dict[str, Optional[int]]:
    usage = getattr(response, "usage_metadata", None) or {}
    if not isinstance(usage, Mapping):
        usage = _json_safe(usage)
    if not isinstance(usage, Mapping):
        return {}
    aliases = {
        "prompt_tokens": ("input_tokens", "prompt_token_count", "promptTokenCount"),
        "candidate_tokens": ("output_tokens", "candidates_token_count", "candidatesTokenCount"),
        "thought_tokens": ("thoughts_token_count", "thoughtsTokenCount"),
        "total_tokens": ("total_tokens", "total_token_count", "totalTokenCount"),
    }
    result: Dict[str, Optional[int]] = {}
    for target, keys in aliases.items():
        value = next((usage.get(key) for key in keys if usage.get(key) is not None), None)
        try:
            result[target] = int(value) if value is not None else None
        except (TypeError, ValueError):
            result[target] = None
    return result


def _response_metadata(response: Any) -> tuple[Optional[str], Optional[str], Dict[str, Any]]:
    metadata = getattr(response, "response_metadata", None) or {}
    safe = _json_safe(metadata)
    if not isinstance(safe, dict):
        safe = {}
    response_id = getattr(response, "id", None) or safe.get("response_id")
    finish_reason = safe.get("finish_reason")
    if finish_reason is None:
        finish_reason = safe.get("finishReason")
    return (
        str(response_id) if response_id else None,
        str(finish_reason) if finish_reason else None,
        safe,
    )


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


def _parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError("LLM_TRACE_GCS_URI must start with gs://")
    path = uri[5:]
    bucket, _, prefix = path.partition("/")
    if not bucket:
        raise ValueError("LLM_TRACE_GCS_URI must name a bucket")
    return bucket, prefix.strip("/")


def _persist_payload(
    *,
    service: str,
    run_id: str,
    sequence: int,
    call_id: str,
    payload: Dict[str, Any],
) -> str:
    from google.cloud import storage  # type: ignore

    uri = (os.getenv("LLM_TRACE_GCS_URI") or "").strip()
    bucket_name, prefix = _parse_gcs_uri(uri)
    object_parts = [
        part
        for part in (
            prefix,
            "llm-traces",
            "v1",
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
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(object_name)
    blob.metadata = {
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "service": service,
        "run_id": run_id,
        "retention_days": "30",
    }
    blob.upload_from_string(content, content_type="application/gzip")
    return f"gs://{bucket_name}/{object_name}"


def _model_config(
    *,
    timeout_seconds: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> tuple[str, str, float, int]:
    config = llm_runtime_config()
    timeout = (
        config.default_timeout_seconds
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    retries = config.max_retries if max_retries is None else int(max_retries)
    return config.model, config.location, timeout, retries


class LLMTraceSession:
    """Mutable per-run trace accumulator; snapshots are immutable JSON values."""

    def __init__(
        self,
        *,
        service: str,
        run_id: str,
        initial: Optional[Mapping[str, Any]] = None,
        sink: Optional[TraceSink] = None,
    ) -> None:
        self.service = service
        self.run_id = run_id
        self.sink = sink
        self._lock = threading.RLock()
        self._calls: List[LLMCallDiagnostic] = []
        if initial:
            try:
                parsed = LLMRunDiagnostics.model_validate(initial)
                self._calls = [item.model_copy(deep=True) for item in parsed.calls]
            except Exception:
                self._calls = []

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            calls = [item.model_copy(deep=True) for item in self._calls]
        failed = [item for item in calls if item.status == "failed"]
        recovered = [item for item in calls if item.status == "recovered"]
        active = next((item for item in reversed(calls) if item.status == "started"), None)
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
        diagnostics = LLMRunDiagnostics(
            service=self.service,
            run_id=self.run_id,
            status=status,
            current_call_id=active.call_id if active else None,
            total_calls=len(calls),
            succeeded_calls=sum(
                item.status in {"succeeded", "recovered"} for item in calls
            ),
            recovered_calls=len(recovered),
            failed_calls=len(failed),
            contract_failed_calls=sum(
                item.status == "failed" and item.failure_stage != "transport"
                for item in calls
            ),
            payload_capture_enabled=config.payload_capture_enabled,
            payload_storage_configured=config.payload_storage_configured,
            payload_persistence_failures=sum(
                item.payload_persistence_status == "failed" for item in calls
            ),
            calls=calls,
        )
        return diagnostics.model_dump(mode="json")

    def mark_recovered(
        self,
        call_id: str,
        *,
        disposition: str = "recovered_by_format_repair",
    ) -> None:
        """Mark a failed contract response as losslessly recovered downstream."""
        with self._lock:
            diagnostic = next(
                (item for item in self._calls if item.call_id == call_id),
                None,
            )
            if diagnostic is None or diagnostic.status != "failed":
                raise ValueError(f"Failed LLM call not found: {call_id}")
            diagnostic.status = "recovered"
            diagnostic.disposition = disposition
        _emit_structured(
            "llm_call_recovered",
            service=self.service,
            run_id=self.run_id,
            call_id=call_id,
            disposition=disposition,
        )
        self._notify()

    def _notify(self) -> None:
        if self.sink is None:
            return
        try:
            self.sink(self.snapshot())
        except Exception as exc:
            _emit_structured(
                "llm_trace_sink_failed",
                level=logging.ERROR,
                service=self.service,
                run_id=self.run_id,
                error_type=type(exc).__name__,
                error_message=_sanitize_error(exc),
            )
            if getattr(self.sink, "requires_persistence", False):
                raise

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

    def _start_call(
        self,
        *,
        messages: Sequence[Any],
        node: str,
        operation: str,
        artifact_type: Optional[str],
        artifact_id: Optional[str],
        correction_attempt: int,
        timeout_seconds: Optional[float],
        max_retries: Optional[int],
    ) -> tuple[LLMCallDiagnostic, float, List[Dict[str, Any]]]:
        serialized_messages = serialize_messages(messages)
        prompt_text = "\n".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True)
            for item in serialized_messages
        )
        model, location, timeout, retries = _model_config(
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        with self._lock:
            sequence = len(self._calls) + 1
            call_id = f"llm-{sequence:04d}-{uuid.uuid4().hex[:8]}"
            config = observability_config()
            diagnostic = LLMCallDiagnostic(
                call_id=call_id,
                sequence=sequence,
                service=self.service,
                run_id=self.run_id,
                node=node,
                operation=operation,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                correction_attempt=correction_attempt,
                model=model,
                location=location,
                configured_timeout_seconds=timeout,
                configured_max_retries=retries,
                started_at=_utc_now(),
                prompt_message_count=len(messages),
                prompt_character_count=len(prompt_text),
                prompt_sha256=_sha256(prompt_text),
                payload_persistence_status="pending" if config.payload_capture_enabled else "disabled",
            )
            self._calls.append(diagnostic)
        _emit_structured(
            "llm_call_started",
            service=self.service,
            run_id=self.run_id,
            call_id=diagnostic.call_id,
            sequence=diagnostic.sequence,
            node=node,
            operation=operation,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            correction_attempt=correction_attempt,
            model=model,
            location=location,
            prompt_message_count=len(messages),
            prompt_character_count=len(prompt_text),
            prompt_sha256=diagnostic.prompt_sha256,
        )
        self._notify()
        return diagnostic, time.perf_counter(), serialized_messages

    def _finish_payload(
        self,
        diagnostic: LLMCallDiagnostic,
        payload: Dict[str, Any],
    ) -> None:
        payload["diagnostic"] = diagnostic.model_dump(mode="json")
        config = observability_config()
        if not config.payload_capture_enabled:
            diagnostic.payload_persistence_status = "disabled"
            return
        if not config.payload_storage_configured:
            diagnostic.payload_persistence_status = "failed"
            _emit_structured(
                "llm_payload_persistence_failed",
                level=logging.ERROR,
                service=self.service,
                run_id=self.run_id,
                call_id=diagnostic.call_id,
                failure_code="payload_storage_not_configured",
            )
            return
        try:
            uri = _persist_payload(
                service=self.service,
                run_id=self.run_id,
                sequence=diagnostic.sequence,
                call_id=diagnostic.call_id,
                payload=payload,
            )
            diagnostic.payload_persistence_status = "stored"
            _emit_structured(
                "llm_payload_persisted",
                service=self.service,
                run_id=self.run_id,
                call_id=diagnostic.call_id,
                payload_uri=uri,
            )
        except Exception as exc:
            diagnostic.payload_persistence_status = "failed"
            _emit_structured(
                "llm_payload_persistence_failed",
                level=logging.ERROR,
                service=self.service,
                run_id=self.run_id,
                call_id=diagnostic.call_id,
                failure_code="payload_persistence_failed",
                error_type=type(exc).__name__,
                error_message=_sanitize_error(exc),
            )

    def _fail(
        self,
        diagnostic: LLMCallDiagnostic,
        *,
        started: float,
        failure_code: str,
        stage: str,
        exc: Exception,
        payload: Dict[str, Any],
    ) -> None:
        diagnostic.status = "failed"
        diagnostic.failure_code = failure_code
        diagnostic.failure_stage = stage
        diagnostic.error_type = type(exc).__name__
        diagnostic.error_message = _sanitize_error(exc)
        diagnostic.completed_at = _utc_now()
        diagnostic.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        payload["error"] = {
            "type": type(exc).__name__,
            "message": _sanitize_error(exc),
            "stage": stage,
            "failure_code": failure_code,
        }
        self._finish_payload(diagnostic, payload)
        _emit_structured(
            "llm_response_contract_failed" if stage != "transport" else "llm_call_failed",
            level=logging.ERROR,
            **diagnostic.model_dump(mode="json"),
        )
        self._notify()

    def invoke_json(
        self,
        *,
        model: Any,
        messages: Sequence[Any],
        node: str,
        operation: str,
        validator: Callable[[Dict[str, Any]], T],
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        correction_attempt: int = 0,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        task_id: Optional[str] = None,
        batch_id: Optional[str] = None,
    ) -> TracedLLMResult[T]:
        diagnostic, started, serialized_messages = self._start_call(
            messages=messages,
            node=node,
            operation=operation,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            correction_attempt=correction_attempt,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        private_payload: Dict[str, Any] = {
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            "diagnostic": diagnostic.model_dump(mode="json"),
            "request": {"messages": serialized_messages},
            "response": None,
            "processing": {},
        }
        try:
            response = model.invoke(list(messages))
            diagnostic.transport_status = "succeeded"
        except Exception as exc:
            diagnostic.transport_status = "failed"
            private_payload["error"] = {
                "type": type(exc).__name__,
                "message": _sanitize_error(exc),
            }
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_transport_error",
                stage="transport",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_transport_error",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="transport",
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                task_id=task_id,
                batch_id=batch_id,
            ) from exc
        try:
            raw_text, shape, raw_content = extract_response_text(response)
            diagnostic.response_extraction_status = "passed"
            diagnostic.response_content_shape = shape
            diagnostic.response_character_count = len(raw_text)
            diagnostic.response_sha256 = _sha256(raw_text)
            response_id, finish_reason, response_metadata = _response_metadata(response)
            diagnostic.provider_response_id = response_id
            diagnostic.finish_reason = finish_reason
            diagnostic.token_usage = _token_usage(response)
            private_payload["response"] = {
                "raw_content": raw_content,
                "normalized_text": raw_text,
                "content_shape": shape,
                "response_metadata": response_metadata,
                "usage_metadata": _json_safe(getattr(response, "usage_metadata", None)),
                "response_id": response_id,
            }
        except Exception as exc:
            diagnostic.response_extraction_status = "failed"
            private_payload["response"] = {
                "raw_content": _json_safe(getattr(response, "content", response))
            }
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_empty_or_unreadable_response",
                stage="response_extraction",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_empty_or_unreadable_response",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="response_extraction",
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                task_id=task_id,
                batch_id=batch_id,
            ) from exc
        try:
            payload = parse_json_object(raw_text)
            diagnostic.json_parse_status = "passed"
            private_payload["processing"]["json"] = payload
        except Exception as exc:
            diagnostic.json_parse_status = "failed"
            for field, value in inspect_json_structure(raw_text, exc).items():
                setattr(diagnostic, field, value)
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_invalid_json",
                stage="json_parse",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_invalid_json",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="json_parse",
                raw_text=raw_text,
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                task_id=task_id,
                batch_id=batch_id,
            ) from exc
        try:
            value = validator(payload)
            diagnostic.contract_validation_status = "passed"
            private_payload["processing"]["validated_value"] = _json_safe(value)
        except Exception as exc:
            diagnostic.contract_validation_status = "failed"
            private_payload["processing"]["validation_error"] = {
                "type": type(exc).__name__,
                "message": _sanitize_error(exc),
            }
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_response_contract_error",
                stage="contract_validation",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_response_contract_error",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="contract_validation",
                artifact_type=artifact_type,
                artifact_id=artifact_id,
                task_id=task_id,
                batch_id=batch_id,
            ) from exc
        diagnostic.status = "succeeded"
        diagnostic.completed_at = _utc_now()
        diagnostic.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        self._finish_payload(diagnostic, private_payload)
        _emit_structured("llm_call_succeeded", **diagnostic.model_dump(mode="json"))
        self._notify()
        return TracedLLMResult(
            call_id=diagnostic.call_id,
            raw_text=raw_text,
            payload=payload,
            value=value,
        )

    def invoke_text(
        self,
        *,
        model: Any,
        messages: Sequence[Any],
        node: str,
        operation: str,
        validator: Optional[Callable[[str], T]] = None,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        correction_attempt: int = 0,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
    ) -> TracedLLMResult[Any]:
        return self._invoke_text_impl(
            model=model,
            messages=messages,
            node=node,
            operation=operation,
            validator=validator,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            correction_attempt=correction_attempt,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )

    def _invoke_text_impl(
        self,
        *,
        model: Any,
        messages: Sequence[Any],
        node: str,
        operation: str,
        validator: Optional[Callable[[str], T]],
        artifact_type: Optional[str],
        artifact_id: Optional[str],
        correction_attempt: int,
        timeout_seconds: Optional[float],
        max_retries: Optional[int],
    ) -> TracedLLMResult[Any]:
        diagnostic, started, serialized_messages = self._start_call(
            messages=messages,
            node=node,
            operation=operation,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            correction_attempt=correction_attempt,
            timeout_seconds=timeout_seconds,
            max_retries=max_retries,
        )
        private_payload: Dict[str, Any] = {
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            "diagnostic": diagnostic.model_dump(mode="json"),
            "request": {"messages": serialized_messages},
            "response": None,
            "processing": {},
        }
        try:
            response = model.invoke(list(messages))
            diagnostic.transport_status = "succeeded"
        except Exception as exc:
            diagnostic.transport_status = "failed"
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_transport_error",
                stage="transport",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_transport_error",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="transport",
            ) from exc
        try:
            raw_text, shape, raw_content = extract_response_text(response)
            diagnostic.response_extraction_status = "passed"
            diagnostic.response_content_shape = shape
            diagnostic.response_character_count = len(raw_text)
            diagnostic.response_sha256 = _sha256(raw_text)
            response_id, finish_reason, response_metadata = _response_metadata(response)
            diagnostic.provider_response_id = response_id
            diagnostic.finish_reason = finish_reason
            diagnostic.token_usage = _token_usage(response)
            private_payload["response"] = {
                "raw_content": raw_content,
                "normalized_text": raw_text,
                "content_shape": shape,
                "response_metadata": response_metadata,
                "usage_metadata": _json_safe(getattr(response, "usage_metadata", None)),
                "response_id": response_id,
            }
        except Exception as exc:
            diagnostic.response_extraction_status = "failed"
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_empty_or_unreadable_response",
                stage="response_extraction",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_empty_or_unreadable_response",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="response_extraction",
            ) from exc
        try:
            value = validator(raw_text) if validator is not None else raw_text
            diagnostic.contract_validation_status = "passed"
            private_payload["processing"]["validated_value"] = _json_safe(value)
        except Exception as exc:
            diagnostic.contract_validation_status = "failed"
            self._fail(
                diagnostic,
                started=started,
                failure_code="llm_response_contract_error",
                stage="contract_validation",
                exc=exc,
                payload=private_payload,
            )
            raise LLMCallError(
                failure_code="llm_response_contract_error",
                call_id=diagnostic.call_id,
                node=node,
                operation=operation,
                stage="contract_validation",
            ) from exc
        diagnostic.status = "succeeded"
        diagnostic.completed_at = _utc_now()
        diagnostic.duration_ms = max(0, int((time.perf_counter() - started) * 1000))
        self._finish_payload(diagnostic, private_payload)
        _emit_structured("llm_call_succeeded", **diagnostic.model_dump(mode="json"))
        self._notify()
        return TracedLLMResult(
            call_id=diagnostic.call_id,
            raw_text=raw_text,
            value=value,
        )


def get_trace_session(
    *,
    service: str,
    run_id: str,
    initial: Optional[Mapping[str, Any]] = None,
) -> LLMTraceSession:
    current = _CURRENT_SESSION.get()
    if current is not None and current.service == service and current.run_id == run_id:
        return current
    return LLMTraceSession(service=service, run_id=run_id, initial=initial)


@contextmanager
def llm_trace_session(
    *,
    service: str,
    run_id: str,
    initial: Optional[Mapping[str, Any]] = None,
    sink: Optional[TraceSink] = None,
) -> Iterator[LLMTraceSession]:
    session = LLMTraceSession(service=service, run_id=run_id, initial=initial, sink=sink)
    token = _CURRENT_SESSION.set(session)
    try:
        yield session
    finally:
        _CURRENT_SESSION.reset(token)
