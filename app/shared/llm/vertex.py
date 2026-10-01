"""The only code in the app that talks to Gemini on Vertex AI, through google-genai.

One client is kept per project, location and header set. SDK retries are off: the LLM client decides what to
retry and records every attempt. Each request carries its own timeout.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from app.shared.config import resolve_project, configured_project

from .profiles import ModelProfile
from .protocol import FilePart, LLMRequest, LLMResponse
from .vertex_schema import compile_schema, wire_schema
from .errors import ProviderError


def outcome_from(reason):
    if reason == "STOP":
        return "completed"
    if reason == "MAX_TOKENS":
        return "truncated"
    if reason in {"BLOCKED", "SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII",
                  "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT", "MODEL_ARMOR"}:
        return "blocked"
    return "unknown"


def provider_error(exc):
    from .errors import is_transient
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, ImportError):
        return ProviderError(str(exc), kind="configuration")
    if isinstance(exc, ValueError):
        return ProviderError(str(exc), kind="request")
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    if isinstance(exc, (DefaultCredentialsError, RefreshError)):
        return ProviderError(str(exc), kind="authentication")
    from google.genai import errors
    if isinstance(exc, errors.APIError):
        kind = "authentication" if exc.code in {401, 403} else "request" if exc.code in {400, 404, 422} else "transport"
        transient = exc.code in {408, 429, 500, 502, 503, 504}
        return ProviderError(str(exc), kind=kind, transient=transient)
    return ProviderError(str(exc), transient=is_transient(exc))


def usage_from(metadata: Dict[str, Any]) -> Dict[str, Optional[int]]:
    return {
        "prompt_tokens": metadata.get("prompt_token_count"),
        "candidate_tokens": metadata.get("candidates_token_count"),
        "thought_tokens": metadata.get("thoughts_token_count"),
        "total_tokens": metadata.get("total_token_count"),
    }


def reply_from(response: Any) -> LLMResponse:
    """The first candidate's text without thought parts, and a normalized, explicit outcome."""
    raw = response.model_dump(mode="json", exclude_none=True)
    candidates = raw.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    parts = (candidate.get("content") or {}).get("parts") or []
    reason = candidate.get("finish_reason")
    blocked = (raw.get("prompt_feedback") or {}).get("block_reason")
    return LLMResponse(
        text="".join(part.get("text", "") for part in parts if not part.get("thought")),
        outcome="blocked" if blocked else outcome_from(str(reason or "")),
        finish_reason=str(reason or blocked) if reason or blocked else None,
        usage=usage_from(raw.get("usage_metadata") or {}),
        response_id=raw.get("response_id"),
        model_version=raw.get("model_version"),
        raw=raw,
    )


class VertexProvider:
    name = "vertex_ai"

    def __init__(self, store=None) -> None:
        self.store = store
        self._clients: Dict[tuple, Any] = {}
        self._lock = threading.Lock()

    def prepare_profile(self, profile, *, configure=True):
        from dataclasses import replace
        if profile.project:
            return profile
        try:
            project = resolve_project() if configure else configured_project()
        except RuntimeError:
            # Freeze the absence too: metadata and skipped nodes need no model access. An actual
            # call fails with a configuration error; it never re-reads a changed environment.
            project = None
        return replace(profile, project=project)

    def _client(self, profile: ModelProfile) -> Any:
        from google import genai
        from google.genai import types

        project = profile.project
        if not project:
            raise ProviderError("Model project was not resolved at client creation", kind="configuration")
        key = (project, profile.location, profile.headers)
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = genai.Client(
                    vertexai=True,
                    project=project,
                    location=profile.location,
                    http_options=types.HttpOptions(
                        headers=dict(profile.headers) or None,
                        retry_options=types.HttpRetryOptions(attempts=1),
                    ),
                )
                self._clients[key] = client
        return client

    def _contents(self, request: LLMRequest) -> Any:
        from google.genai import types

        files = [part for part in request.parts if isinstance(part, FilePart)]
        if len(files) > 12 or sum(part.reference['size'] for part in files) > 50_000_000:
            raise ProviderError("Image request exceeds the selected adapter's limits", kind="request")
        parts = [
            types.Part.from_uri(file_uri=self._file_uri(part), mime_type=part.mime_type)
            if isinstance(part, FilePart)
            else types.Part.from_text(text=part)
            for part in request.parts
        ]
        return types.Content(role="user", parts=parts)

    @staticmethod
    def _generation(profile: ModelProfile, request: LLMRequest) -> Dict[str, Any]:
        from google.genai import types

        settings: Dict[str, Any] = {
            "temperature": profile.temperature,
            "max_output_tokens": request.max_output_tokens or profile.max_output_tokens,
            "candidate_count": profile.candidate_count,
            "media_resolution": profile.media_resolution,
            "response_mime_type": "application/json" if request.json_output else None,
            "response_schema": compile_schema(request.response_schema, profile),
        }
        if profile.thinking_level is not None:
            settings["thinking_config"] = types.ThinkingConfig(
                thinking_level=profile.thinking_level, include_thoughts=profile.include_thoughts
            )
        return {key: value for key, value in settings.items() if value is not None}

    @staticmethod
    def _http(profile: ModelProfile, request: LLMRequest) -> Any:
        from google.genai import types

        timeout = request.timeout_seconds or profile.timeout_seconds
        return types.HttpOptions(timeout=int(timeout * 1000))

    def generate(self, profile: ModelProfile, request: LLMRequest) -> LLMResponse:
        try:
            from google.genai import types

            config = types.GenerateContentConfig(
                http_options=self._http(profile, request),
                system_instruction=request.system,
                **self._generation(profile, request),
            )
            response = self._client(profile).models.generate_content(
                model=profile.model, contents=self._contents(request), config=config
            )
            return reply_from(response)
        except ProviderError:
            raise
        except Exception as exc:
            raise provider_error(exc) from exc


    def count_tokens(self, profile: ModelProfile, request: LLMRequest) -> int:
        try:
            from google.genai import types

            config = types.CountTokensConfig(
                http_options=self._http(profile, request),
                system_instruction=request.system,
                generation_config=types.GenerationConfig(**self._generation(profile, request)),
            )
            response = self._client(profile).models.count_tokens(
                model=profile.model, contents=self._contents(request), config=config
            )
            if response.total_tokens is None:
                raise ProviderError("Vertex did not return an input token count", kind="request")
            return int(response.total_tokens)
        except ProviderError:
            raise
        except Exception as exc:
            raise provider_error(exc) from exc


    def _file_uri(self, part):
        from app.shared.runs.factory import object_store
        store = self.store or object_store(part.reference["namespace"])
        if part.mime_type not in {"image/png", "image/jpeg", "image/webp"} or not 0 < part.reference["size"] <= 30_000_000:
            raise ProviderError("Unsupported original image type or size", kind="request")
        if not callable(getattr(store, "model_uri", None)):
            raise ProviderError("This object store cannot supply original files to Vertex", kind="request")
        return store.model_uri(part.reference)

    def schema_for(self, profile, request):
        """The SDK's JSON form, used to audit the schema actually carried on the wire."""
        return wire_schema(request.response_schema, profile)

    def measurement_payload(self, profile, request):
        # Preserve MFI's text-only admission calculation; other callers can measure original images too.
        try:
            parts = [{"file_data": {"file_uri": self._file_uri(part), "mime_type": part.mime_type}}
                     if isinstance(part, FilePart) else {"text": part} for part in request.parts]
            payload = {"model": profile.model, "contents": [{"role": "user", "parts": parts}],
                       "generation_config": {"response_schema": compile_schema(request.response_schema, profile),
                                             "response_mime_type": "application/json" if request.json_output else None,
                                             "temperature": profile.temperature,
                                             "max_output_tokens": request.max_output_tokens or profile.max_output_tokens}}
            if request.system is not None:
                payload["system_instruction"] = request.system
            return payload
        except ProviderError:
            raise
        except Exception as exc:
            raise provider_error(exc) from exc

    def dependencies(self):
        from importlib.metadata import PackageNotFoundError, version
        try:
            return {"google-genai": version("google-genai")}
        except PackageNotFoundError:
            return {"google-genai": None}
