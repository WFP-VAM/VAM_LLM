"""Node trend_analyst: the market trend, from the statistics, the events and the basket facts."""
from __future__ import annotations

import logging
from typing import Dict, Any, Mapping

from ..prompts import trend_analysis_prompt
from ..state import MarketReportState
from ..runtime import llm_client
from ..basket_context import _mapping, build_basket_context

logger = logging.getLogger(__name__)


def _trend_with_basket_identity(value: Any, basket_context: Mapping[str, Any]) -> Dict[str, Any]:
    trend = _mapping(value)
    generated = _mapping(trend.get("basket_analysis"))
    basket_analysis: Dict[str, Any] = {"primary": None, "secondary": None}
    for role in ("primary", "secondary"):
        role_context = _mapping(basket_context.get(role))
        if not role_context:
            continue
        raw = _mapping(generated.get(role))
        basket_analysis[role] = {
            "role": role,
            "basket_name": role_context.get("basket_name"),
            "scope_type": role_context.get("scope_type"),
            "scope_label": role_context.get("scope_label"),
            "trajectory": str(raw.get("trajectory") or "unknown"),
            "movement_observations": [str(item) for item in (raw.get("movement_observations") or [])],
            "cost_composition_observations": [
                str(item) for item in (raw.get("cost_composition_observations") or [])
            ],
        }
    trend["basket_analysis"] = basket_analysis
    return trend


def node_trend_analyst(state: MarketReportState) -> dict:
    """Nodo: Analizza i trend."""
    logger.info("[TrendAnalyst] Analyzing trends")
    
    stats = state.get("data_statistics", {})
    events = state.get("events", [])
    basket_context = build_basket_context(state)
    llm = llm_client(state)
    trace = llm.tracer
    
    prompt = trend_analysis_prompt(stats, events, basket_context)
    
    def _validate_trend(result: Dict[str, Any]) -> Dict[str, Any]:
        if result.get("trajectory") not in {
            "increasing_prices",
            "decreasing_prices",
            "stable",
            "volatile",
        }:
            raise ValueError("trajectory is missing or invalid")
        for field in ("key_market_drivers",):
            if not isinstance(result.get(field), list):
                raise ValueError(f"{field} must be a list")
        for field in ("commodity_analysis", "regional_analysis", "basket_analysis"):
            if not isinstance(result.get(field), dict):
                raise ValueError(f"{field} must be an object")
        if not isinstance(result.get("outlook"), str) or not result["outlook"].strip():
            raise ValueError("outlook must be non-empty text")
        return result

    traced = llm.generate_json(
        prompt=prompt,
        node="trend_analyst",
        operation="market_monitor.trend_analysis.v1",
        artifact_type="analysis",
        artifact_id="trend_analysis",
        validator=_validate_trend,
    )
    trend_analysis = traced.value
    llm_calls = 1
    trend_analysis = _trend_with_basket_identity(trend_analysis, basket_context)
    
    return {
        "trend_analysis": trend_analysis,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "trend_analyst"
    }
