"""Node red_team: the QA review of the drafted sections, with flags for the correction loop."""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from ..i18n import prompt_base_context
from ..prompts import render_prompt, TERMINOLOGY_THRESHOLDS, _json_for_prompt
from ..state import MarketReportState, _state_language
from ..runtime import llm_client
from ..basket_context import build_basket_context, optional_module_basket_relevance
from ..qa import _normalized_qa_flags, qa_review_from_state
from ..modules import AVAILABLE_MODULES

logger = logging.getLogger(__name__)


def node_red_team(state: MarketReportState) -> dict:
    """Nodo: Quality Assurance - verifica il draft."""
    logger.info("[RedTeam] Fact-checking draft")
    
    llm = llm_client(state)
    trace = llm.tracer
    language = _state_language(state)
    sections = state.get("report_draft_sections", {})
    stats = state.get("data_statistics", {})
    trend = state.get("trend_analysis", {}) or {}
    exchange_data = state.get("exchange_rate_data", {}) or {}
    basket_context = build_basket_context(state)
    module_relevance = {
        module_id: optional_module_basket_relevance(state, module_id)
        for module_id in AVAILABLE_MODULES
    }
     
    if not sections:
        trace.record_skip(
            node="red_team",
            operation="market_monitor.red_team_review.v1",
            reason="no_report_sections",
        )
        review = qa_review_from_state({**dict(state), "skeptic_flags": []})
        return {
            "skeptic_flags": [],
            "qa_review": review,
            "correction_targets": [],
            "llm_diagnostics": trace.snapshot(),
            "current_node": "red_team",
        }
     
    draft_text = "\n\n".join([f"== {k} ==\n{v}" for k, v in sections.items()])
     
    prompt = render_prompt(
        "red_team",
        language,
        {
            **prompt_base_context(language),
            "stats_json": _json_for_prompt(stats),
            "exchange_mom": exchange_data.get("monthly_change_pct"),
            "exchange_yoy": exchange_data.get("yearly_change_pct"),
            "exchange_trend": exchange_data.get("trend"),
            "exchange_data_json": _json_for_prompt(exchange_data) if exchange_data else "None",
            "trend_json": _json_for_prompt(trend),
            "terminology_thresholds_json": _json_for_prompt(TERMINOLOGY_THRESHOLDS),
            "basket_context_json": _json_for_prompt(basket_context),
            "module_basket_relevance_json": _json_for_prompt(module_relevance),
            "draft_text": draft_text,
        },
    )

     
    def _validate_qa(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        flags = result.get("flags")
        if not isinstance(flags, list):
            raise ValueError("flags must be a list")
        required = {
            "section",
            "claim",
            "issue_type",
            "severity",
            "details",
            "recommendation",
        }
        for index, flag in enumerate(flags):
            if not isinstance(flag, dict):
                raise ValueError("every QA flag must be an object")
            missing = sorted(required - set(flag))
            if missing:
                raise ValueError(f"flags[{index}] is missing: {', '.join(missing)}")
            if flag.get("severity") not in {"high", "medium", "low"}:
                raise ValueError(f"flags[{index}].severity is invalid")
            for field in required - {"severity"}:
                if not isinstance(flag.get(field), str):
                    raise ValueError(f"flags[{index}].{field} must be text")
        return _normalized_qa_flags(flags)

    traced = llm.generate_json(
        prompt=prompt,
        node="red_team",
        operation="market_monitor.red_team_review.v1",
        artifact_type="global",
        artifact_id="qa_review",
        correction_attempt=int(state.get("correction_attempts", 0) or 0),
        validator=_validate_qa,
    )
    flags = traced.value
    llm_calls = 1

    review = qa_review_from_state({**dict(state), "skeptic_flags": flags})
    return {
        "skeptic_flags": flags,
        "qa_review": review,
        "correction_targets": [],
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "red_team"
    }
