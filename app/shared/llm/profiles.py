"""Each drafter's model settings, in one place."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .settings import llm_runtime_config


@dataclass(frozen=True)
class ModelProfile:
    """How one drafter talks to the model. A request may override the timeout and the output cap."""

    service: str
    model: str
    location: str
    temperature: float
    timeout_seconds: float
    project: Optional[str] = None  # None: resolved from the environment, then application-default credentials
    max_output_tokens: Optional[int] = None
    candidate_count: Optional[int] = None
    thinking_level: Optional[str] = None
    include_thoughts: Optional[bool] = None
    media_resolution: Optional[str] = None
    attempts: int = 1  # automatic attempts on transient errors; the SDK itself never retries
    retry_delay_seconds: float = 4.0
    headers: Tuple[Tuple[str, str], ...] = ()
    provider: str = "vertex_ai"
    schema_policy: str = "declaration"
    max_characters: Optional[int] = None
    max_input_tokens: Optional[int] = None
    count_timeout_seconds: float = 30.0
    count_attempts: int = 2
    count_retry_delay_seconds: float = 1.0


def market_monitor_profile() -> ModelProfile:
    """From the LLM_* settings. LLM_MAX_RETRIES has always counted attempts, as LangChain read it: 2 is one retry."""
    config = llm_runtime_config()
    return ModelProfile(
        service="market-monitor",
        model=config.model,
        location=config.location,
        temperature=0.0,
        timeout_seconds=config.default_timeout_seconds,
        max_output_tokens=config.max_output_tokens,
        candidate_count=1,
        attempts=max(1, config.max_retries),
    )


def mfi_profile() -> ModelProfile:
    return ModelProfile(service="mfi-drafter", model="gemini-3.1-pro-preview", location="global",
                        temperature=1.0, timeout_seconds=600, max_output_tokens=65_536,
                        candidate_count=1, schema_policy="mfi", max_characters=1_200_000,
                        max_input_tokens=250_000)


def service_profiles(service: str, *, settings=None, timeout=None) -> dict[str, ModelProfile]:
    """Resolve once per client. Keys are logical roles, never model names in the services."""
    from dataclasses import replace
    if service == "mfi-drafter":
        profile = mfi_profile()
        return {"text": profile, "summary": replace(profile, timeout_seconds=180)}
    if service == "market-monitor":
        return {"text": market_monitor_profile()}
    if service == "seasonal-outlook":
        from app.shared.seasonal import SeasonalSettings
        settings = settings or SeasonalSettings.from_env()
        if settings.environment:
            settings = SeasonalSettings.from_env()  # Resolve model configuration for this operation, including a manual retry.
        if not settings.project:
            raise ValueError("Explicit Seasonal project required")
        if settings.location != "global":
            raise ValueError("Seasonal Gemini location must be global")
        common = ModelProfile(service=service, model=settings.model, location=settings.location,
                              project=settings.project, temperature=1.0, timeout_seconds=timeout or 600,
                              thinking_level="HIGH", include_thoughts=False,
                              media_resolution="MEDIA_RESOLUTION_HIGH", attempts=2,
                              headers=(("X-Vertex-AI-LLM-Request-Type", "shared"),))
        return {"evidence": replace(common, max_output_tokens=32768),
                "report": replace(common, max_output_tokens=65536)}
    raise ValueError(f"Unknown model service: {service}")


def operation_profile(service: str, operation: str, profiles: dict[str, ModelProfile]) -> ModelProfile:
    if service == "mfi-drafter":
        key = "summary" if operation == "mfi.light.executive_summary.v1" else "text"
    elif service == "seasonal-outlook":
        stage = operation.removeprefix("seasonal_outlook.").removesuffix(".v1")
        if stage not in {"extraction", "review", "refinement", "feedback", "draft", "report_review", "redraft"}:
            raise ValueError(f"Unknown Seasonal operation: {operation}")
        key = "evidence" if stage in {"extraction", "review", "refinement", "feedback"} else "report"
    else:
        key = "text"
    return profiles[key]
