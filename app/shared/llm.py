"""Validated, cache-safe Vertex LLM configuration."""
from __future__ import annotations

import math
import os
from typing import Any, Dict, Literal, Optional, Tuple

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_vertexai import ChatVertexAI
from pydantic import BaseModel, Field


load_dotenv()

DEFAULT_LLM_TIMEOUT_SECONDS = 90.0
DEFAULT_LLM_MAX_RETRIES = 2
MAX_LLM_TIMEOUT_SECONDS = 600.0
MAX_LLM_RETRIES = 10


class LLMRuntimeConfigurationError(ValueError):
    """Stable failure raised before a report run uses invalid LLM settings."""

    code = "llm_runtime_configuration_invalid"

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")

    def to_public_dict(self) -> Dict[str, str]:
        return {"code": self.code, "field": self.field, "message": self.message}


class LLMRuntimeConfig(BaseModel):
    """Effective non-secret model runtime settings."""

    model: str
    location: str
    default_timeout_seconds: float = Field(gt=0, le=MAX_LLM_TIMEOUT_SECONDS)
    max_retries: int = Field(ge=0, le=MAX_LLM_RETRIES)
    max_output_tokens: Optional[int] = Field(default=None, gt=0)


class LLMRuntimeStatus(BaseModel):
    """Sanitized configuration status exposed by service metadata."""

    configuration_status: Literal["configured", "invalid"]
    default_timeout_seconds: Optional[float] = None
    max_retries: Optional[int] = None
    error_code: Optional[str] = None
    error_field: Optional[str] = None


_model_instances: Dict[Tuple[Any, ...], BaseChatModel] = {}


def _get_vertex_project_id() -> str:
    project_id = (os.getenv("VERTEX_PROJECT_ID") or "").strip()
    if project_id:
        return project_id

    for key in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT"):
        candidate = (os.getenv(key) or "").strip()
        if candidate:
            return candidate

    try:
        import google.auth  # type: ignore

        _, inferred_project_id = google.auth.default()
        if inferred_project_id:
            return str(inferred_project_id).strip()
    except Exception:
        pass

    raise RuntimeError(
        "Missing Vertex project id. Set VERTEX_PROJECT_ID (recommended) or "
        "ensure GOOGLE_CLOUD_PROJECT is set."
    )


def _float_setting(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise LLMRuntimeConfigurationError(name, "must be a number") from exc
    if not math.isfinite(value) or value <= 0 or value > MAX_LLM_TIMEOUT_SECONDS:
        raise LLMRuntimeConfigurationError(
            name,
            f"must be greater than 0 and at most {MAX_LLM_TIMEOUT_SECONDS:g}",
        )
    return value


def _int_setting(
    name: str,
    default: Optional[int],
    *,
    minimum: int,
    maximum: Optional[int] = None,
) -> Optional[int]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LLMRuntimeConfigurationError(name, "must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        upper = f" and at most {maximum}" if maximum is not None else ""
        raise LLMRuntimeConfigurationError(
            name,
            f"must be at least {minimum}{upper}",
        )
    return value


def llm_runtime_config() -> LLMRuntimeConfig:
    """Parse and strictly validate effective LLM settings."""
    retries = _int_setting(
        "LLM_MAX_RETRIES",
        DEFAULT_LLM_MAX_RETRIES,
        minimum=0,
        maximum=MAX_LLM_RETRIES,
    )
    max_output_tokens = _int_setting(
        "LLM_MAX_OUTPUT_TOKENS",
        None,
        minimum=1,
    )
    return LLMRuntimeConfig(
        model=(os.getenv("LLM_MODEL") or "gemini-2.5-pro").strip(),
        location=(os.getenv("VERTEX_LOCATION") or "us-central1").strip(),
        default_timeout_seconds=_float_setting(
            "LLM_TIMEOUT_SECONDS",
            DEFAULT_LLM_TIMEOUT_SECONDS,
        ),
        max_retries=int(retries if retries is not None else DEFAULT_LLM_MAX_RETRIES),
        max_output_tokens=max_output_tokens,
    )


def llm_runtime_status() -> LLMRuntimeStatus:
    """Return non-secret status without raising from info or health endpoints."""
    try:
        config = llm_runtime_config()
    except LLMRuntimeConfigurationError as exc:
        return LLMRuntimeStatus(
            configuration_status="invalid",
            error_code=exc.code,
            error_field=exc.field,
        )
    return LLMRuntimeStatus(
        configuration_status="configured",
        default_timeout_seconds=config.default_timeout_seconds,
        max_retries=config.max_retries,
    )


def get_model(
    *,
    timeout_seconds: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> BaseChatModel:
    """Return a Vertex model cached by every effective invocation setting."""
    config = llm_runtime_config()
    timeout = (
        config.default_timeout_seconds
        if timeout_seconds is None
        else float(timeout_seconds)
    )
    retries = config.max_retries if max_retries is None else int(max_retries)
    if timeout <= 0 or timeout > MAX_LLM_TIMEOUT_SECONDS:
        raise LLMRuntimeConfigurationError(
            "timeout_seconds",
            f"must be greater than 0 and at most {MAX_LLM_TIMEOUT_SECONDS:g}",
        )
    if retries < 0 or retries > MAX_LLM_RETRIES:
        raise LLMRuntimeConfigurationError(
            "max_retries",
            f"must be at least 0 and at most {MAX_LLM_RETRIES}",
        )

    project = _get_vertex_project_id()
    key = (
        config.model,
        project,
        config.location,
        timeout,
        retries,
        config.max_output_tokens,
    )
    if key not in _model_instances:
        _model_instances[key] = ChatVertexAI(
            model_name=config.model,
            project=project,
            location=config.location,
            temperature=0,
            timeout=timeout,
            max_retries=retries,
            max_output_tokens=config.max_output_tokens,
        )
    return _model_instances[key]
