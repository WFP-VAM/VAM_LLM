"""Node context_retrieval: external context for the MFI report, ReliefWeb and Seerist documents, no model calls."""
from __future__ import annotations

import logging
from typing import Any, Dict

from app.shared.context.news import gather_context
from app.shared.context.retrievers import ReliefWebRetriever, SeeristRetriever
from ..context_status import resolve_context_status
from ..evidence import source_map

logger = logging.getLogger(__name__)

TOPIC_TERMS = ["market functionality", "food security", "supply", "availability", "access"]


def retrieve_context_documents(state: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch, merge and de-duplicate context documents for the assessment period."""
    country = state.get("country", "")
    logger.info(f"[ContextRetrieval] Fetching context for {country}")

    context = gather_context(
        ReliefWebRetriever(verbose=False),
        SeeristRetriever(verbose=False),
        country=country,
        start_date=state.get("data_collection_start", ""),
        end_date=state.get("data_collection_end", ""),
        reliefweb_terms=TOPIC_TERMS,
        seerist_terms=TOPIC_TERMS,
        seerist_focus=["market functionality", "market", "availability", "access", "food security"],
        limit=8,
    )
    docs = context.documents
    statuses = {name: context.status(name) for name in ("ReliefWeb", "Seerist")}
    return {
        "contextual_documents": docs,
        "document_references": context.references(),
        "seerist_documents": list(context.seerist),
        "reliefweb_documents": list(context.reliefweb),
        "context_counts": context.counts(),
        "context_status": resolve_context_status(retriever_statuses=statuses, documents=docs, statements=[],
                                                 extraction_mode="not_started").model_dump(),
        "retriever_traces": context.traces,
    }


def retrieve_context(base):
    try:
        retrieved = retrieve_context_documents(base)
    except Exception as exc:
        # Context has always been optional. Keep its failure distinct from analysis.
        retrieved = {"contextual_documents": [], "retriever_traces": [{"retriever": "context", "error": type(exc).__name__}],
                     "context_status": resolve_context_status(retriever_statuses={"ReliefWeb":"failed", "Seerist":"failed"}, documents=[]).model_dump()}
    sources = source_map(retrieved.get("contextual_documents", []))
    context_status = dict(retrieved.get("context_status") or {})
    for legacy_field in ("statements_classified", "final_accepted_statements", "extraction_mode", "classification_outcome", "unresolved_statement_count"):
        context_status.pop(legacy_field, None)
    context_status.update(status="available" if sources else context_status.get("status", "unavailable"),
                          classification_mode="integrated_drafting_and_review", total_documents=len(sources))
    return {**{k: retrieved.get(k, []) for k in ("contextual_documents", "retriever_traces", "seerist_documents", "reliefweb_documents")},
        "context_status": context_status, "sources": sources,
        "context_limitation": None if sources else "No usable external context was retrieved; interpretation relies on the MFI assessment.",
        "document_references": [{**{k:v for k,v in source.items() if k != "content"},
            "original_document_id": source.get("doc_id"), "doc_id": key, "source_id": key} for key,source in sources.items()]}
