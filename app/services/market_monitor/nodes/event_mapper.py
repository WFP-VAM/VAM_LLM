"""Node event_mapper: the key market events of the retrieved documents."""
from __future__ import annotations

import logging
from typing import List, Dict, Any

from ..prompts import event_extraction_prompt
from ..state import MarketReportState
from ..runtime import llm_client

logger = logging.getLogger(__name__)


def node_event_mapper(state: MarketReportState) -> dict:
    """Nodo: Estrae eventi dai documenti."""
    logger.info("[EventMapper] Extracting events")
    
    documents = state.get("documents", [])
    llm = llm_client(state)
    trace = llm.tracer
    
    if not documents:
        trace.record_skip(
            node="event_mapper",
            operation="market_monitor.event_extraction.v1",
            reason="no_contextual_documents",
        )
        # Fallback events
        events = [{
            "event_id": "evt_fallback",
            "category": "economic",
            "statement": f"Ongoing price increases in {state['country']} due to economic factors.",
            "location": state["country"],
            "date": state["time_period"] + "-01",
            "source_ids": []
        }]
        return {
            "events": events,
            "llm_diagnostics": trace.snapshot(),
            "current_node": "event_mapper",
        }
    
    # Prepare context
    context = "\n\n".join([
        f"[{d['doc_id']}] {d['date']}: {d['content'][:500]}"
        for d in documents[:5]
    ])
    
    prompt = event_extraction_prompt(state['country'], context)
    
    def _validate_events(result: Dict[str, Any]) -> List[Dict[str, Any]]:
        events = result.get("events")
        if not isinstance(events, list):
            raise ValueError("events must be a list")
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise ValueError(f"events[{index}] must be an object")
            for field in ("event_id", "category", "statement", "location", "date"):
                if not str(event.get(field) or "").strip():
                    raise ValueError(f"events[{index}].{field} is required")
            if not isinstance(event.get("source_ids"), list):
                raise ValueError(f"events[{index}].source_ids must be a list")
        return events

    traced = llm.generate_json(
        prompt=prompt,
        node="event_mapper",
        operation="market_monitor.event_extraction.v1",
        artifact_type="context",
        artifact_id="events",
        validator=_validate_events,
    )
    events = traced.value
    llm_calls = 1
    
    if not events:
        events = [{
            "event_id": "evt_fallback",
            "category": "economic",
            "statement": f"Market conditions in {state['country']} remain challenging.",
            "location": state["country"],
            "date": state["time_period"] + "-01",
            "source_ids": []
        }]
    
    return {
        "events": events,
        "llm_calls": state.get("llm_calls", 0) + llm_calls,
        "llm_diagnostics": trace.snapshot(),
        "current_node": "event_mapper"
    }
