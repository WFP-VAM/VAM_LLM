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
