"""External context for the MFI report: ReliefWeb and Seerist documents, no model calls."""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List

from app.shared.retrievers import ReliefWebRetriever, SeeristRetriever
from .context_status import resolve_context_status

logger = logging.getLogger(__name__)

TOPIC_TERMS = ["market functionality", "food security", "supply", "availability", "access"]


def retrieve_context_documents(state: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch, merge and de-duplicate context documents for the assessment period."""
    country = state.get("country", "")
    start_date = state.get("data_collection_start", "")
    end_date = state.get("data_collection_end", "")
    logger.info(f"[ContextRetrieval] Fetching context for {country}")

    reliefweb = ReliefWebRetriever(verbose=False)
    reliefweb_docs = reliefweb.fetch(country=country, start_date=start_date, end_date=end_date, max_records=8,
                                     query=ReliefWebRetriever.build_economy_query(extra_terms=TOPIC_TERMS))
    seerist = SeeristRetriever(verbose=False)
    seerist_queries = [
        SeeristRetriever.build_lucene_or_query(list(SeeristRetriever.DEFAULT_ECON_TERMS) + TOPIC_TERMS),
        SeeristRetriever.build_lucene_or_query(["market functionality", "market", "availability", "access", "food security"]),
        "",
    ]
    seerist_docs = seerist.fetch_batch(queries=seerist_queries, start_date=start_date, end_date=end_date,
                                       country=country, max_per_query=8)[:8]
    retriever_traces = [trace for trace in (getattr(reliefweb, "last_trace", None), getattr(seerist, "last_trace", None)) if trace]

    docs: List[Dict[str, Any]] = []
    seen = set()
    for doc in [*reliefweb_docs, *seerist_docs]:
        key = (doc.get("url") or "").strip() or doc.get("doc_id")
        if not key or key in seen:
            continue
        seen.add(key)
        if not doc.get("content"):
            doc["content"] = doc.get("title", "")
        docs.append(doc)

    statuses = {}
    for name, documents, retriever in (("ReliefWeb", reliefweb_docs, reliefweb), ("Seerist", seerist_docs, seerist)):
        trace = getattr(retriever, "last_trace", None) or {}
        statuses[name] = "failed" if trace.get("error") else "completed" if documents else "no_results"
    counts = Counter(d.get("source", "Unknown") for d in docs)
    return {
        "contextual_documents": docs,
        "document_references": [{k: d.get(k) for k in ("doc_id", "source", "title", "url", "date")} for d in docs],
        "seerist_documents": list(seerist_docs),
        "reliefweb_documents": list(reliefweb_docs),
        "context_counts": {"Seerist": int(counts.get("Seerist", 0)), "ReliefWeb": int(counts.get("ReliefWeb", 0)),
                           "total": len(docs)},
        "context_status": resolve_context_status(retriever_statuses=statuses, documents=docs, statements=[],
                                                 extraction_mode="not_started").model_dump(),
        "retriever_traces": retriever_traces,
    }
