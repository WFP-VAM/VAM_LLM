"""Node highlights_drafter: the report's highlights."""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from ..i18n import format_currency_value, prompt_base_context, t
from ..prompts import render_prompt, TERMINOLOGY_THRESHOLDS, _json_for_prompt, _report_month_for_prompt
from ..state import MarketReportState, _state_currency_code, _state_language
from ..runtime import llm_client
from ..text import _normalize_output_text, format_pct
from ..basket_context import build_basket_context
from ..qa import _correction_flags_json, _targeted

logger = logging.getLogger(__name__)


def _has_currency_depreciation_driver(drivers: Any) -> bool:
    if not drivers:
        return False
    try:
        for d in drivers:
            s = str(d or "").lower()
            if "currency" in s and (
                "depreciat" in s
                or "devalu" in s
                or "weak" in s
                or "collapse" in s
            ):
                return True
            if "fx" in s and "depreciat" in s:
                return True
    except Exception:
        return False
    return False


def node_highlights_drafter(state: MarketReportState) -> dict:
    """Nodo: Genera la sezione Highlights."""
    logger.info("[HighlightsDrafter] Generating highlights")

    if state.get("correction_targets") and not _targeted(state, "HIGHLIGHTS"):
        return {"current_node": "highlights_drafter"}
    
    language = _state_language(state)
    stats = state.get("data_statistics", {})
    trend = state.get("trend_analysis", {})
    exchange_data = state.get("exchange_rate_data", {}) or {}
    currency_code = _state_currency_code(state)
    basket_context = build_basket_context(state)
    llm = llm_client(state)
    trace = llm.tracer
 
    validation_warnings: List[str] = []
    if exchange_data and exchange_data.get("trend") == "stable":
        drivers = (trend or {}).get("key_market_drivers") or []
        if _has_currency_depreciation_driver(drivers):
            validation_warnings.append(
                t(language, "warning.exchange_stable_driver")
            )
     
    # Format statistics with arrows
    formatted_stats = {}
    if stats.get("food_basket"):
        fb = stats["food_basket"]
        formatted_stats["food_basket"] = {
            "current_price": format_currency_value(fb.get("current_price"), currency_code, language),
            "mom_change": format_pct(fb.get("mom_change_pct"), language),
            "yoy_change": format_pct(fb.get("yoy_change_pct"), language),
        }
    
    for name, data in stats.get("commodities", {}).items():
        formatted_stats[name] = {
            "current_price": format_currency_value(data.get("current_price"), currency_code, language),
            "mom_change": format_pct(data.get("mom_change_pct"), language),
            "yoy_change": format_pct(data.get("yoy_change_pct"), language),
        }
    
    prompt = render_prompt(
        "highlights",
        language,
        {
            **prompt_base_context(language),
            "country": state["country"],
            "time_period": state["time_period"],
            "report_month_localized": _report_month_for_prompt(state),
            "formatted_stats_json": _json_for_prompt(formatted_stats),
            "exchange_data_json": _json_for_prompt(exchange_data) if exchange_data else "None",
            "trend_json": _json_for_prompt(trend),
            "terminology_thresholds_json": _json_for_prompt(TERMINOLOGY_THRESHOLDS),
            "validation_warnings_json": _json_for_prompt(validation_warnings),
            "basket_context_json": _json_for_prompt(basket_context),
            "correction_flags_json": _correction_flags_json(state, "HIGHLIGHTS"),
        },
    )
    
    def _validate_highlights(result: Dict[str, Any]) -> Dict[str, Any]:
        value = result.get("HIGHLIGHTS")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("HIGHLIGHTS must be non-empty text")
        normalized, warnings = _normalize_output_text(value, state)
        if not normalized.strip():
            raise ValueError("HIGHLIGHTS is empty after normalization")
        return {"text": normalized, "warnings": warnings}

    traced = llm.generate_json(
        prompt=prompt,
        node="highlights_drafter",
        operation="market_monitor.highlights_drafting.v1",
        artifact_type="report_section",
        artifact_id="HIGHLIGHTS",
        correction_attempt=int(state.get("correction_attempts", 0) or 0),
        validator=_validate_highlights,
    )
    highlights = traced.value["text"]
    validation_warnings.extend(traced.value["warnings"])
    llm_calls = 1
    
    sections = dict(state.get("report_draft_sections") or {})
    sections["HIGHLIGHTS"] = highlights
    
    updates: Dict[str, Any] = {
        "report_draft_sections": sections,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "highlights_drafter"
    }
    if validation_warnings:
        updates["warnings"] = validation_warnings
    return updates
