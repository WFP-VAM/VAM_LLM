"""The only code in the app that talks to Gemini on Vertex AI, through google-genai.

One client is kept per project, location and header set. SDK retries are off: the LLM client decides what to
retry and records every attempt. Each request carries its own timeout.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from app.shared.config import resolve_project

from .profiles import ModelProfile
from .protocol import FilePart, LLMRequest, LLMResponse


def usage_from(metadata: Dict[str, Any]) -> Dict[str, Optional[int]]:
    return {
        "prompt_tokens": metadata.get("prompt_token_count"),
        "candidate_tokens": metadata.get("candidates_token_count"),
        "thought_tokens": metadata.get("thoughts_token_count"),
        "total_tokens": metadata.get("total_token_count"),
    }


def reply_from(response: Any) -> LLMResponse:
    """The first candidate's text without thought parts. No candidate, or no finish reason, reads as BLOCKED."""
    raw = response.model_dump(mode="json", exclude_none=True)
    candidates = raw.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    parts = (candidate.get("content") or {}).get("parts") or []
    return LLMResponse(
        text="".join(part.get("text", "") for part in parts if not part.get("thought")),
        finish_reason=str(candidate.get("finish_reason") or "BLOCKED"),
        usage=usage_from(raw.get("usage_metadata") or {}),
        response_id=raw.get("response_id"),
        model_version=raw.get("model_version"),
        raw=raw,
    )


class VertexProvider:
    def __init__(self) -> None:
        self._clients: Dict[tuple, Any] = {}
        self._lock = threading.Lock()

    def _client(self, profile: ModelProfile) -> Any:
        from google import genai
        from google.genai import types

        project = profile.project or resolve_project()
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

    @staticmethod
    def _contents(request: LLMRequest) -> Any:
        from google.genai import types

        parts = [
            types.Part.from_uri(file_uri=part.uri, mime_type=part.mime_type)
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
            "response_schema": request.response_schema,
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

    def count_tokens(self, profile: ModelProfile, request: LLMRequest) -> int:
        from google.genai import types

        config = types.CountTokensConfig(
            http_options=self._http(profile, request),
            system_instruction=request.system,
            generation_config=types.GenerationConfig(**self._generation(profile, request)),
        )
        response = self._client(profile).models.count_tokens(
            model=profile.model, contents=self._contents(request), config=config
        )
        return int(response.total_tokens or 0)
