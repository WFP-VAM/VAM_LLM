"""Every model call of the app goes through LLMClient: bounded retries on transient errors, one record per attempt."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from types import MappingProxyType
import threading
import time
from typing import Any, Callable, Dict, Optional

from .errors import CONTRACT, EMPTY, INVALID_JSON, TRANSPORT, TRUNCATED, LLMCallError, ProviderError, is_transient
from .profiles import ModelProfile, operation_profile, service_profiles
from .protocol import FilePart, LLMRequest, LLMResponse, LLMResult, RequestMeasurement
from .schema import inspect_json_structure, parse_json_object
from .tracing import TRACE_SCHEMA_VERSION, Tracer, _json_safe, _sanitize_error
_PROVIDERS = {}
_PROVIDER_LOCK = threading.Lock()


def default_provider(name="vertex_ai", *, store=None):
    """Only the selected adapter is imported. Provider instances are reused per object resolver."""
    if name != "vertex_ai":
        raise ProviderError(f"Unknown LLM provider: {name}", kind="configuration")
    from .vertex import VertexProvider
    key = (name, id(store))
    with _PROVIDER_LOCK:
        if key not in _PROVIDERS:
            _PROVIDERS[key] = VertexProvider(store=store)
        return _PROVIDERS[key]


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def create_llm_client(service, *, tracer=None, provider=None, provider_registry=None,
                      settings=None, timeout=None, store=None, profiles=None, sleep=None, configure=True):
    resolved = dict(profiles) if profiles is not None else service_profiles(service, settings=settings, timeout=timeout)
    if not resolved or any(p.service != service for p in resolved.values()):
        raise ProviderError("Invalid service model profiles", kind="configuration")
    return LLMClient(next(iter(resolved.values())), tracer=tracer, provider=provider,
                     profiles=resolved, provider_registry=provider_registry, store=store, sleep=sleep, configure=configure)


def describe_llm_config(service, *, settings=None):
    """Informational settings without credential discovery; execution resolves and freezes credentials' project."""
    return create_llm_client(service, settings=settings, configure=False).describe()


def _request_payload(profile: ModelProfile, request: LLMRequest) -> Dict[str, Any]:
    """The request as captured for private payload tracing (opt-in)."""
    return {
        "model": profile.model,
        "location": profile.location,
        "temperature": profile.temperature,
        "max_output_tokens": request.max_output_tokens or profile.max_output_tokens,
        "system": request.system,
        "parts": [
            {"object": dict(part.reference), "mime_type": part.mime_type} if isinstance(part, FilePart) else part
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
        sleep: Optional[Callable[[float], None]] = None,
        profiles=None, provider_registry=None, store=None, configure=True,
    ) -> None:
        supplied = dict(profiles or {"text": profile})
        registry = dict(provider_registry or {})
        self._providers = {}
        for key, item in supplied.items():
            selected = provider or registry.get(item.provider) or default_provider(item.provider, store=store)
            prepare = getattr(selected, "prepare_profile", None)
            if prepare:
                item = prepare(item, configure=configure)
            if item.attempts < 1 or item.count_attempts < 1:
                raise ProviderError("Attempt limits must be positive", kind="configuration")
            self._providers[key] = selected
            supplied[key] = replace(item, provider=getattr(selected, "name", "injected"))
        self.profiles = MappingProxyType(supplied)
        self.profile = next(iter(supplied.values()))
        self._routed = profiles is not None
        self._count_cache = {}
        self._count_lock = threading.RLock()
        self.tracer = tracer or Tracer(service=profile.service, run_id=f"{profile.service}-direct")
        self.provider = next(iter(self._providers.values()))
        self._sleep = sleep or time.sleep

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
        profile = self.profile_for(request)
        attempts = max(1, profile.attempts)
        failed: list[str] = []
        for attempt in range(1, attempts + 1):
            try:
                result = self._attempt(request, parse, validate, failed[-1] if failed else request.retry_of)
            except LLMCallError as error:
                if error.stage != "transport" or not error.transient or attempt == attempts:
                    raise
                failed.append(error.call_id)
                self._sleep(profile.retry_delay_seconds)
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
        profile = self.profile_for(request)
        provider = self.provider_for(profile)
        if not callable(getattr(provider, "count_tokens", None)):
            raise ProviderError("This model cannot count input tokens", kind="configuration")
        count = provider.count_tokens(profile, request)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProviderError("The provider returned an invalid token count", kind="request")
        return count

    def _attempt(self, request, parse, validate, retry_of: Optional[str]) -> LLMResult:
        tracer, profile = self.tracer, self.profile_for(request)
        provider = self.provider_for(profile)
        # Schema compilation is local and must succeed before an inference attempt is started.
        schema = self._transport_schema(profile, provider, request)
        payload: Dict[str, Any] = {
            "trace_schema_version": TRACE_SCHEMA_VERSION,
            "request": _request_payload(profile, request),
            "response": None,
            "processing": {},
        }
        record = tracer.start(profile, request, retry_of=retry_of, transport_schema_hash=_digest(schema) if schema is not None else None)

        def fail(code: str, stage: str, error: BaseException, *, transient: bool = False,
                 raw_text: Optional[str] = None, fields: Optional[Dict[str, Any]] = None) -> LLMCallError:
            tracer.failed(record, code=code, stage=stage, error=error, payload=payload,
                          transient=transient if stage == "transport" else None, fields=fields)
            return LLMCallError(failure_code=code, call_id=record.call_id, node=request.node,
                                operation=request.operation, stage=stage, transient=transient, raw_text=raw_text,
                                artifact_type=request.artifact_type, artifact_id=request.artifact_id)

        try:
            response = provider.generate(profile, request)
        except Exception as exc:
            raise fail("llm_" + exc.kind + "_error" if isinstance(exc, ProviderError) and exc.kind != "transport" else TRANSPORT,
                       "transport", exc, transient=is_transient(exc)) from exc
        tracer.responded(record, response)
        text = response.text.strip()
        payload["response"] = {
            "text": text,
            "outcome": response.outcome,
            "finish_reason": response.finish_reason,
            "usage": response.usage,
            "response_id": response.response_id,
            "model_version": response.model_version,
            "raw": response.raw,
        }
        if response.outcome in {"blocked", "unknown"}:
            error = ValueError(f"Model response is {response.outcome}")
            raise fail("llm_response_" + response.outcome, "response_extraction", error) from error
        if not text:
            error = ValueError(f"LLM response contains no text (finish reason {response.finish_reason})")
            raise fail(EMPTY, "response_extraction", error, fields={"response_extraction_status": "failed"}) from error
        if request.fail_on_truncation and response.outcome == "truncated":
            error = ValueError(f"LLM response stopped at {response.finish_reason}")
            raise fail(TRUNCATED, "response_extraction", error, fields={"response_extraction_status": "failed"}) from error
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


    def profile_for(self, request):
        return operation_profile(self.profile.service, request.operation, self.profiles) if self._routed else self.profile

    def provider_for(self, profile):
        return next(self._providers[key] for key, item in self.profiles.items() if item is profile)

    @staticmethod
    def _transport_schema(profile, provider, request):
        compile_schema = getattr(provider, "schema_for", None)
        return compile_schema(profile, request) if compile_schema else request.response_schema

    def describe(self):
        profiles = {}
        dependencies = {}
        for key, profile in self.profiles.items():
            profiles[key] = {name: getattr(profile, name) for name in (
                "provider", "model", "location", "project", "temperature", "timeout_seconds", "max_output_tokens",
                "max_characters", "max_input_tokens", "count_timeout_seconds", "attempts",
                "thinking_level", "include_thoughts", "media_resolution", "candidate_count", "schema_policy")}
            dependency_info = getattr(self._providers[key], "dependencies", None)
            if dependency_info:
                dependencies.update(dependency_info())
        result = {"profiles": profiles, "dependencies": dependencies, "sdk_retries": 0}
        for field in ("model", "location", "provider"):
            values = {item[field] for item in profiles.values()}
            result[field] = next(iter(values)) if len(values) == 1 else None
        return result

    def measure(self, request, *, on_count=None):
        """Measure and cache the exact input token count once per effective request in this run."""
        profile = self.profile_for(request)
        provider = self.provider_for(profile)
        builder = getattr(provider, "measurement_payload", None)
        payload = builder(profile, request) if builder else _request_payload(profile, request)
        characters = len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        key = _digest({"profile": asdict(profile), "request": payload, "contract": request.response_schema,
                       "system": request.system, "parts": _request_payload(profile, request)["parts"]})
        result = dict(characters=characters, fingerprint=key, max_characters=profile.max_characters,
                      max_input_tokens=profile.max_input_tokens)
        if profile.max_characters is not None and characters > profile.max_characters:
            return RequestMeasurement(input_tokens=None, **result)
        with self._count_lock:
            if key not in self._count_cache:
                if not callable(getattr(provider, "count_tokens", None)):
                    raise ProviderError("This model cannot count input tokens", kind="configuration")
                for attempt in range(profile.count_attempts):
                    if on_count:
                        on_count()
                    try:
                        self._count_cache[key] = self.count_tokens(replace(request, timeout_seconds=profile.count_timeout_seconds))
                        break
                    except Exception as exc:
                        if attempt + 1 == profile.count_attempts or not is_transient(exc):
                            raise
                        self._sleep(profile.count_retry_delay_seconds)
            return RequestMeasurement(input_tokens=self._count_cache[key], **result)
