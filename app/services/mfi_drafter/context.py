"""External context for the MFI report: ReliefWeb and Seerist documents, no model calls."""
from __future__ import annotations

import logging
from typing import Any, Dict

from app.shared.context.news import gather_context
from app.shared.context.retrievers import ReliefWebRetriever, SeeristRetriever
from .context_status import resolve_context_status

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
