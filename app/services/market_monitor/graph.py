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

from typing import Literal, Dict, Any, Optional, Callable

from langgraph.graph import StateGraph, END

from .state import MarketReportState
from .qa import _material_qa_flags
from .nodes.data_agent import node_data_agent
from .nodes.graph_designer import node_graph_designer
from .nodes.news_retrieval import node_news_retrieval
from .nodes.event_mapper import node_event_mapper
from .nodes.trend_analyst import node_trend_analyst
from .nodes.module_orchestrator import node_module_orchestrator
from .nodes.highlights_drafter import node_highlights_drafter
from .nodes.narrative_drafter import node_narrative_drafter
from .nodes.red_team import node_red_team
from .nodes.prepare_correction import node_prepare_correction

OnStepCallback = Callable[[str, Dict[str, Any]], None]


# ============================================================================
# ROUTING & GRAPH BUILDER
# ============================================================================

MAX_CORRECTION_ATTEMPTS = 3


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
