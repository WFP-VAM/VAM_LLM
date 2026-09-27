"""
Market Monitor - Graph
======================
Workflow LangGraph per generazione Market Monitor Reports.

Struttura del grafo:
    data_agent → graph_designer → news_retrieval → event_mapper 
    → trend_analyst → module_orchestrator → highlights_drafter 
    → narrative_drafter → red_team → [loop/END]
"""
from __future__ import annotations

import logging
from typing import Literal, List, Dict, Any, Optional, Callable, Mapping

from langgraph.graph import StateGraph, END

from app.shared.llm import log_llm_run_summary, tracing_run

from .i18n import format_currency_value, prompt_base_context, resolve_report_language, t
from .prompts import render_prompt, TERMINOLOGY_THRESHOLDS, _json_for_prompt, _report_month_for_prompt
from .state import MarketReportState, _state_currency_code, _state_language, create_initial_state
from .runtime import llm_client
from .text import _normalize_output_text, format_pct
from .basket_context import build_basket_context, optional_module_basket_relevance
from .qa import (
    _correction_flags_json,
    _correction_targets,
    _material_qa_flags,
    _normalized_qa_flags,
    _targeted,
    normalize_qa_review,
    qa_review_from_state,
)
from .modules import AVAILABLE_MODULES
from .nodes.data_agent import node_data_agent
from .nodes.graph_designer import node_graph_designer
from .nodes.news_retrieval import node_news_retrieval
from .nodes.data_agent import node_data_agent
from .nodes.event_mapper import node_event_mapper
from .nodes.graph_designer import node_graph_designer
from .nodes.module_orchestrator import node_module_orchestrator
from .nodes.news_retrieval import node_news_retrieval
from .nodes.trend_analyst import node_trend_analyst

logger = logging.getLogger(__name__)

OnStepCallback = Callable[[str, Dict[str, Any]], None]


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

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


# ============================================================================
# NODE: HIGHLIGHTS DRAFTER
# ============================================================================

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


# ============================================================================
# NODE: NARRATIVE DRAFTER
# ============================================================================

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

# NODE: RED TEAM (QA)
# ============================================================================

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


# ============================================================================
# ROUTING & GRAPH BUILDER
# ============================================================================

MAX_CORRECTION_ATTEMPTS = 3


def node_prepare_correction(state: MarketReportState) -> dict:
    """Capture material QA targets without clearing the flags that explain them."""
    targets = _correction_targets(state.get("skeptic_flags") or [])
    return {
        "correction_targets": targets,
        "correction_attempts": int(state.get("correction_attempts") or 0) + 1,
        "current_node": "prepare_correction",
    }


def should_correct(state: MarketReportState) -> Literal["correct", "finish"]:
    """Determina se servono correzioni."""
    flags = _material_qa_flags(state.get("skeptic_flags", []))
    attempts = state.get("correction_attempts", 0)
    
    if flags and attempts < MAX_CORRECTION_ATTEMPTS:
        return "correct"
    return "finish"


def build_graph(on_step: Optional[OnStepCallback] = None):
    """Costruisce il grafo LangGraph per Market Monitor."""
    
    def wrap_node(node_name: str, fn):
        def wrapped(state: MarketReportState):
            state_dict = dict(state)
            if on_step is not None:
                on_step(node_name, state_dict)

            updates = fn(state)

            if on_step is not None:
                merged = dict(state_dict)
                if isinstance(updates, dict):
                    merged.update(updates)
                on_step(node_name, merged)

            return updates

        return wrapped

    graph = StateGraph(MarketReportState)
    
    # Add nodes
    graph.add_node("data_agent", wrap_node("data_agent", node_data_agent))
    graph.add_node("graph_designer", wrap_node("graph_designer", node_graph_designer))
    graph.add_node("news_retrieval", wrap_node("news_retrieval", node_news_retrieval))
    graph.add_node("event_mapper", wrap_node("event_mapper", node_event_mapper))
    graph.add_node("trend_analyst", wrap_node("trend_analyst", node_trend_analyst))
    graph.add_node("module_orchestrator", wrap_node("module_orchestrator", node_module_orchestrator))
    graph.add_node("highlights_drafter", wrap_node("highlights_drafter", node_highlights_drafter))
    graph.add_node("narrative_drafter", wrap_node("narrative_drafter", node_narrative_drafter))
    graph.add_node("red_team", wrap_node("red_team", node_red_team))
    graph.add_node("prepare_correction", wrap_node("prepare_correction", node_prepare_correction))
    
    # Set entry point
    graph.set_entry_point("data_agent")
    
    # Linear flow
    graph.add_edge("data_agent", "graph_designer")
    graph.add_edge("graph_designer", "news_retrieval")
    graph.add_edge("news_retrieval", "event_mapper")
    graph.add_edge("event_mapper", "trend_analyst")
    graph.add_edge("trend_analyst", "module_orchestrator")
    graph.add_edge("module_orchestrator", "highlights_drafter")
    graph.add_edge("highlights_drafter", "narrative_drafter")
    graph.add_edge("narrative_drafter", "red_team")
    graph.add_edge("prepare_correction", "module_orchestrator")
    
    # QA Loop
    graph.add_conditional_edges(
        "red_team",
        should_correct,
        {
            "correct": "prepare_correction",
            "finish": END
        }
    )
    
    return graph.compile()


# ============================================================================
# PUBLIC API
# ============================================================================

def run_report_generation(
    country: str,
    time_period: str,
    commodity_list: List[str],
    admin1_list: List[str],
    currency_code: str = "USD",
    enabled_modules: List[str] = None,
    basket_version_id: Optional[str] = None,
    basket_selection: Optional[Any] = None,
    previous_report_text: str = "",
    use_mock_data: bool = False,
    language: str = "auto",
    on_step: Optional[OnStepCallback] = None,
    run_id: Optional[str] = None,
    llm_trace_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> dict:
    """
    Entry point per la generazione del Market Monitor.
    
    Returns:
        Stato finale con report completo
    """
    if enabled_modules is None:
        enabled_modules = ["exchange_rate"]
    language_info = resolve_report_language(country, language)
    if basket_selection is not None and hasattr(basket_selection, "to_metadata"):
        basket_selection_payload = basket_selection.to_metadata()
    elif isinstance(basket_selection, Mapping):
        basket_selection_payload = dict(basket_selection)
    else:
        basket_selection_payload = None
    
    initial_state = create_initial_state(
        country=country,
        time_period=time_period,
        commodity_list=commodity_list,
        admin1_list=admin1_list,
        currency_code=currency_code,
        enabled_modules=enabled_modules,
        basket_version_id=basket_version_id,
        basket_selection=basket_selection_payload,
        previous_report_text=previous_report_text,
        use_mock_data=use_mock_data,
        language=language_info["language"],
        locale=language_info["locale"],
        language_source=language_info["language_source"],
        run_id=run_id,
    )
    
    agent = build_graph(on_step=on_step)
    with tracing_run(
        service="market-monitor",
        run_id=initial_state["run_id"],
        initial=initial_state.get("llm_diagnostics"),
        live=llm_trace_sink,
    ) as trace:
        try:
            result = agent.invoke(initial_state)
        except Exception:
            log_llm_run_summary(trace.snapshot())
            raise
        result["llm_diagnostics"] = trace.snapshot()
        log_llm_run_summary(result["llm_diagnostics"])
    for key in ("databridges_rows", "seerist_documents", "reliefweb_documents"):
        result.pop(key, None)
    result["language"] = language_info["language"]
    result["locale"] = language_info["locale"]
    result["language_source"] = language_info["language_source"]
    result["qa_review"] = normalize_qa_review(result)
    if result["qa_review"]["status"] == "completed_with_warnings":
        warning = t(
            language_info["language"],
            "warning.qa_unresolved",
            count=len(_material_qa_flags(result["qa_review"]["flags"])),
        )
        existing_warnings = list(result.get("warnings") or [])
        if warning not in existing_warnings:
            existing_warnings.append(warning)
        result["warnings"] = existing_warnings
    return result
