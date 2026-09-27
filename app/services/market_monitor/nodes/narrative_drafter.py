"""Node narrative_drafter: the market overview, commodity analysis and regional highlights."""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from ..i18n import prompt_base_context
from ..prompts import render_prompt, _json_for_prompt, _report_month_for_prompt
from ..state import MarketReportState, _state_language
from ..runtime import llm_client
from ..text import _normalize_output_text
from ..basket_context import build_basket_context
from ..qa import _normalized_qa_flags

logger = logging.getLogger(__name__)


def node_narrative_drafter(state: MarketReportState) -> dict:
    """Nodo: Genera le sezioni narrative."""
    logger.info("[NarrativeDrafter] Generating narrative sections")

    language = _state_language(state)
    trend = state.get("trend_analysis", {})
    events = state.get("events", [])
    module_sections = dict(state.get("module_sections") or {})
    basket_context = build_basket_context(state)
    core_sections = ["MARKET_OVERVIEW", "COMMODITY_ANALYSIS", "REGIONAL_HIGHLIGHTS"]
    targets = set(state.get("correction_targets") or [])
    if targets and "GLOBAL" not in targets:
        sections_to_generate = [section for section in core_sections if section in targets]
    else:
        sections_to_generate = list(core_sections)
    two_baskets = bool(basket_context.get("secondary_included"))
    word_ranges = (
        {
            "MARKET_OVERVIEW": "250-325 words",
            "COMMODITY_ANALYSIS": "250-350 words",
            "REGIONAL_HIGHLIGHTS": "200-275 words",
        }
        if two_baskets
        else {
            "MARKET_OVERVIEW": "200-250 words",
            "COMMODITY_ANALYSIS": "200-300 words",
            "REGIONAL_HIGHLIGHTS": "150-200 words",
        }
    )
    correction_flags = _normalized_qa_flags(state.get("skeptic_flags") or [])
    correction_flags = [
        flag for flag in correction_flags if flag["section"] in set(sections_to_generate) | {"GLOBAL"}
    ]
    result: Dict[str, Any] = {}
    llm_calls = 0
    normalization_warnings: List[str] = []
    llm = llm_client(state)
    trace = llm.tracer
    if sections_to_generate:
        prompt = render_prompt(
            "narrative",
            language,
            {
                **prompt_base_context(language),
                "country": state["country"],
                "time_period": state["time_period"],
                "report_month_localized": _report_month_for_prompt(state),
                "trend_json": _json_for_prompt(trend),
                "events_json": _json_for_prompt(events),
                "module_sections_json": _json_for_prompt(module_sections) if module_sections else "None",
                "basket_context_json": _json_for_prompt(basket_context),
                "sections_to_generate_json": _json_for_prompt(sections_to_generate),
                "section_word_ranges_json": _json_for_prompt(word_ranges),
                "correction_flags_json": _json_for_prompt(correction_flags),
            },
        )
        def _validate_sections(payload: Dict[str, Any]) -> Dict[str, Any]:
            normalized_sections: Dict[str, str] = {}
            warnings: List[str] = []
            for section in sections_to_generate:
                value = payload.get(section)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{section} must be non-empty text")
                normalized, section_warnings = _normalize_output_text(value, state)
                if not normalized.strip():
                    raise ValueError(f"{section} is empty after normalization")
                normalized_sections[section] = normalized
                warnings.extend(section_warnings)
            return {"sections": normalized_sections, "warnings": warnings}

        traced = llm.generate_json(
            prompt=prompt,
            node="narrative_drafter",
            operation="market_monitor.narrative_drafting.v1",
            artifact_type="report",
            artifact_id="core_sections",
            correction_attempt=int(state.get("correction_attempts", 0) or 0),
            validator=_validate_sections,
        )
        result = traced.value["sections"]
        normalization_warnings.extend(traced.value["warnings"])
        llm_calls = 1

    sections = dict(state.get("report_draft_sections") or {})
    if result:
        for key, value in result.items():
            if key not in sections_to_generate:
                continue
            if isinstance(value, str):
                normalized, warnings = _normalize_output_text(value, state)
                sections[key] = normalized
                normalization_warnings.extend(warnings)
            else:
                sections[key] = value
    
    # Add module sections
    for module_id, section_text in module_sections.items():
        section_key = f"{module_id.upper()}_ANALYSIS"
        sections[section_key] = section_text
    
    document_references = state.get("document_references", []) or []
    if document_references:
        lines = ["REFERENCES"]
        for ref in document_references:
            doc_id = ref.get("doc_id", "")
            source = ref.get("source", "")
            date = ref.get("date", "")
            title = ref.get("title", "")
            url = ref.get("url", "")
            lines.append(f"[{doc_id}] {source} ({date}) {title}")
            if url:
                lines.append(url)
            lines.append("")
        sections["REFERENCES"] = "\n".join(lines).strip()
    
    updates = {
        "report_draft_sections": sections,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "narrative_drafter"
    }
    if normalization_warnings:
        updates["warnings"] = normalization_warnings
    return updates
