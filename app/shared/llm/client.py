"""Every model call of the app goes through LLMClient: bounded retries on transient errors, one record per attempt."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from .errors import CONTRACT, EMPTY, INVALID_JSON, TRANSPORT, TRUNCATED, LLMCallError, is_transient
from .profiles import ModelProfile
from .protocol import FilePart, LLMRequest, LLMResponse, LLMResult
from .schema import TRUNCATED_FINISH_REASONS, inspect_json_structure, parse_json_object
from .tracing import TRACE_SCHEMA_VERSION, Tracer, _json_safe, _sanitize_error
from .vertex import VertexProvider

_PROVIDER: Optional[VertexProvider] = None
_PROVIDER_LOCK = threading.Lock()


def default_provider() -> VertexProvider:
    """One provider per process, so SDK clients and their connections are reused."""
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = VertexProvider()
        return _PROVIDER


def _request_payload(profile: ModelProfile, request: LLMRequest) -> Dict[str, Any]:
    """The request as captured for private payload tracing (opt-in)."""
    return {
        "model": profile.model,
        "location": profile.location,
        "temperature": profile.temperature,
        "max_output_tokens": request.max_output_tokens or profile.max_output_tokens,
        "system": request.system,
        "parts": [
            {"file_uri": part.uri, "mime_type": part.mime_type} if isinstance(part, FilePart) else part
            for part in request.parts
        ],
        "json_output": request.json_output,
        "response_schema": request.response_schema,
    }


class LLMClient:
    def __init__(
        self,
        profile: ModelProfile,
        *,
        tracer: Optional[Tracer] = None,
        provider: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.profile = profile
        self.tracer = tracer or Tracer(service=profile.service, run_id=f"{profile.service}-direct")
        self.provider = provider or default_provider()
        self._sleep = sleep

    def generate(
        self,
        request: LLMRequest,
        *,
        parse: Optional[Callable[[str], Any]] = None,
        validate: Optional[Callable[[Any, LLMResponse], Any]] = None,
    ) -> LLMResult:
        """Send `request` and check the reply.

        `parse(text)` and then `validate(parsed, response)` run inside the call's record, so a reply they reject
        fails the call with a stable code. Only transient transport errors are retried, up to the profile's
        attempts. When a call succeeds, the failed calls it retried or repaired count as recovered, including
        the one named by `request.retry_of` or `request.repair_of`.
        """
        attempts = max(1, self.profile.attempts)
        failed: list[str] = []
        for attempt in range(1, attempts + 1):
            try:
                result = self._attempt(request, parse, validate, failed[-1] if failed else request.retry_of)
            except LLMCallError as error:
                if error.stage != "transport" or not error.transient or attempt == attempts:
                    raise
                failed.append(error.call_id)
                self._sleep(self.profile.retry_delay_seconds)
                continue
            recovered = [(call_id, "recovered_by_retry") for call_id in [request.retry_of, *failed] if call_id]
            if request.repair_of:
                recovered.append((request.repair_of, "recovered_by_repair"))
            for call_id, disposition in recovered:
                self.tracer.mark_recovered(call_id, disposition=disposition)
            return result
        raise AssertionError("unreachable: the last attempt returns or raises")

    def generate_json(
        self,
        *,
        prompt: str,
        node: str,
        operation: str,
        validator: Callable[[Dict[str, Any]], Any],
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        correction_attempt: int = 0,
    ) -> LLMResult:
        """A single prompt whose reply must hold a JSON object that `validator` accepts. A truncated reply fails."""
        request = LLMRequest(
            operation=operation,
            node=node,
            parts=[prompt],
            fail_on_truncation=True,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            correction_attempt=correction_attempt,
        )
        return self.generate(request, parse=parse_json_object, validate=lambda payload, _response: validator(payload))

    def generate_text(
        self,
        *,
        prompt: str,
        node: str,
        operation: str,
        validator: Optional[Callable[[str], Any]] = None,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        correction_attempt: int = 0,
    ) -> LLMResult:
        """A single prompt answered in prose, checked by `validator` if given. A truncated reply fails."""
        request = LLMRequest(
            operation=operation,
            node=node,
            parts=[prompt],
            fail_on_truncation=True,
            artifact_type=artifact_type,
            artifact_id=artifact_id,
            correction_attempt=correction_attempt,
        )
        return self.generate(request, validate=(lambda text, _response: validator(text)) if validator else None)

    def count_tokens(self, request: LLMRequest) -> int:
        """Input tokens of `request` as the model counts them. Not a model call, so not traced."""
        return int(self.provider.count_tokens(self.profile, request))

    def _attempt(self, request, parse, validate, retry_of: Optional[str]) -> LLMResult:
        tracer, profile = self.tracer, self.profile
        payload: Dict[str, Any] = {
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            "request": _request_payload(profile, request),
            "response": None,
            "processing": {},
        }
        record = tracer.start(profile, request, retry_of=retry_of)

        def fail(code: str, stage: str, error: BaseException, *, transient: bool = False,
                 raw_text: Optional[str] = None, fields: Optional[Dict[str, Any]] = None) -> LLMCallError:
            tracer.failed(record, code=code, stage=stage, error=error, payload=payload,
                          transient=transient if stage == "transport" else None, fields=fields)
            return LLMCallError(failure_code=code, call_id=record.call_id, node=request.node,
                                operation=request.operation, stage=stage, transient=transient, raw_text=raw_text,
                                artifact_type=request.artifact_type, artifact_id=request.artifact_id)

        try:
            response = self.provider.generate(profile, request)
        except Exception as exc:
            raise fail(TRANSPORT, "transport", exc, transient=is_transient(exc)) from exc
        tracer.responded(record, response)
        text = response.text.strip()
        payload["response"] = {
            "text": text,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
            "response_id": response.response_id,
            "model_version": response.model_version,
            "raw": response.raw,
        }
        if not text:
            raise fail(EMPTY, "response_extraction", ValueError("LLM response contains no text"),
                       fields={"response_extraction_status": "failed"})
        if request.fail_on_truncation and response.finish_reason.upper() in TRUNCATED_FINISH_REASONS:
            raise fail(TRUNCATED, "response_extraction", ValueError(f"LLM response stopped at {response.finish_reason}"),
                       fields={"response_extraction_status": "failed"})
        tracer.update(record, response_extraction_status="passed")

        parsed: Any = text
        if parse is not None:
            try:
                parsed = parse(text)
            except Exception as exc:
                raise fail(INVALID_JSON, "json_parse", exc, raw_text=text,
                           fields={"json_parse_status": "failed", **inspect_json_structure(text, exc)}) from exc
            tracer.update(record, json_parse_status="passed")
            payload["processing"]["json"] = parsed
        value = parsed
        if validate is not None:
            try:
                value = validate(parsed, response)
            except Exception as exc:
                payload["processing"]["validation_error"] = {"type": type(exc).__name__, "message": _sanitize_error(exc)}
                raise fail(CONTRACT, "contract_validation", exc, fields={"contract_validation_status": "failed"}) from exc
            tracer.update(record, contract_validation_status="passed")
            payload["processing"]["validated_value"] = _json_safe(value)
        tracer.succeeded(record, payload)
        return LLMResult(call_id=record.call_id, response=response, payload=parsed if parse is not None else None,
                         value=value)
