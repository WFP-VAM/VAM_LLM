"""The model client of a Market Monitor run, recording into the run's trace; tests replace llm_provider."""
from __future__ import annotations

from typing import Dict, Any

from app.shared.llm import (
    LLMClient,
    current_tracer,
    create_llm_client,
)


def llm_provider():
    """The provider Market Monitor's calls go through; tests replace it."""
    return None  # The shared factory selects the adapter; tests may inject one.


def llm_client(state: Dict[str, Any]) -> LLMClient:
    """The model client of this report run, recording into the run's trace."""
    tracer = current_tracer(
        service="market-monitor",
        run_id=str(state.get("run_id") or "market-monitor-direct"),
        initial=state.get("llm_diagnostics"),
    )
    with tracer._lock:
        client = getattr(tracer, '_service_client', None)
        if client is None:
            client = create_llm_client('market-monitor', tracer=tracer, provider=llm_provider())
            tracer._service_client = client
        return client
