"""Node news_retrieval: ReliefWeb and Seerist documents for the report month and the month before, no model calls."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List

from app.shared.context.news import gather_context
from app.shared.context.retrievers import ReliefWebRetriever, SeeristRetriever

from ..state import MarketReportState

logger = logging.getLogger(__name__)


def node_news_retrieval(state: MarketReportState) -> dict:
    """Nodo: Recupera notizie (mock per ora)."""
    logger.info(f"[NewsRetrieval] Fetching news for {state['country']}")

    warnings: List[str] = []

    country = state.get("country", "")
    time_period = state.get("time_period", "")

    try:
        start_dt = datetime.strptime(time_period + "-01", "%Y-%m-%d")
    except Exception:
        start_dt = datetime.utcnow().replace(day=1)

    end_dt = (start_dt + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    prev_month_start = (start_dt - timedelta(days=1)).replace(day=1)

    context = gather_context(
        ReliefWebRetriever(verbose=False),
        SeeristRetriever(verbose=False),
        country=country,
        start_date=prev_month_start.strftime("%Y-%m-%d"),
        end_date=end_dt.strftime("%Y-%m-%d"),
        reliefweb_terms=["food security", "supply", "shortage", "subsidy"],
        seerist_terms=["food security", "wheat", "sorghum", "rice", "cooking oil"],
        seerist_focus=["market", "food security", "inflation", "currency", "availability"],
        limit=10,
    )
    if context.error("Seerist"):
        warnings.append(f"Seerist retrieval unavailable for {country}: {context.error('Seerist')}")

    updates = {
        "documents": context.documents,
        "document_references": context.references(),
        "seerist_documents": list(context.seerist),
        "reliefweb_documents": list(context.reliefweb),
        "news_counts": context.counts(),
        "retriever_traces": context.traces,
        "current_node": "news_retrieval",
    }
    if warnings:
        updates["warnings"] = warnings
    return updates
