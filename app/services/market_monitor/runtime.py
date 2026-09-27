"""The model client of a Market Monitor run, recording into the run's trace; tests replace llm_provider."""
from __future__ import annotations

from typing import Dict, Any

from app.shared.llm import (
    LLMClient,
    current_tracer,
    default_provider,
    market_monitor_profile,
)


def llm_provider():
    """The provider Market Monitor's calls go through; tests replace it."""
    return default_provider()


def llm_client(state: Dict[str, Any]) -> LLMClient:
    """The model client of this report run, recording into the run's trace."""
    tracer = current_tracer(
        service="market-monitor",
        run_id=str(state.get("run_id") or "market-monitor-direct"),
        initial=state.get("llm_diagnostics"),
    )
    return LLMClient(market_monitor_profile(), tracer=tracer, provider=llm_provider())
