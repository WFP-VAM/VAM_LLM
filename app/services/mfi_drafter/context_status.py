"""Pure context-availability resolution for MFI Drafter 2.0.

Provider traces may contain implementation errors and request details.  This module
deliberately consumes only stable provider outcomes and document identities so the
public status cannot leak provider diagnostics or depend on an LLM.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from .schemas import MFIContextRetrieverStatus, MFIContextStatus


DEFAULT_CONTEXT_SOURCES = ("ReliefWeb", "Seerist")
_RETRIEVER_STATES = {"completed", "no_results", "failed", "not_attempted"}
_EXTRACTION_MODES = {
    "not_started",
    "llm",
    "fallback",
    "failed",
    "not_applicable",
    "offline",
}


def _document_key(document: Mapping[str, Any]) -> str:
    return str(document.get("url") or document.get("doc_id") or "").strip()


def _deduplicated_documents(
    documents: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    seen: set[str] = set()
    result: list[Mapping[str, Any]] = []
    for document in documents:
        if not isinstance(document, Mapping):
            continue
        key = _document_key(document)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(document)
    return result


def _retriever_state(value: Any) -> str:
    if isinstance(value, MFIContextRetrieverStatus):
        return value.status
    if isinstance(value, Mapping):
        value = value.get("status")
    status = str(value or "not_attempted")
    return status if status in _RETRIEVER_STATES else "failed"


def accepted_context_statements(
    statements: Sequence[Mapping[str, Any]],
    documents: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Return source-valid, non-unrelated statements that survived QA withdrawal."""
    known_document_ids = {
        str(document.get("doc_id"))
        for document in documents
        if isinstance(document, Mapping) and document.get("doc_id")
    }
    accepted: list[Mapping[str, Any]] = []
    for statement in statements:
        if not isinstance(statement, Mapping):
            continue
        if statement.get("classification") == "unrelated":
            continue
        if bool(statement.get("substituted")):
            continue
        cited = {
            str(document_id)
            for document_id in statement.get("document_ids", []) or []
            if document_id
        }
        if cited & known_document_ids:
            accepted.append(statement)
    return accepted


def not_attempted_context_status(
    sources: Iterable[str] = DEFAULT_CONTEXT_SOURCES,
) -> MFIContextStatus:
    """Build the explicit offline/mock status required by the R7 contract."""
    retrievers = {
        str(source): MFIContextRetrieverStatus(
            status="not_attempted",
            retrieved_document_count=0,
        )
        for source in sorted({str(item) for item in sources}, key=str.casefold)
    }
    return MFIContextStatus(
        status="not_attempted",
        retrievers=retrievers,
        total_deduplicated_documents_retrieved=0,
        statements_classified=0,
        final_accepted_statements=0,
        extraction_mode="offline",
        limitation_code=None,
    )


def resolve_context_status(
    *,
    retriever_statuses: Mapping[str, Any],
    documents: Sequence[Mapping[str, Any]],
    statements: Sequence[Mapping[str, Any]] = (),
    extraction_mode: str = "not_started",
    classification_failed: bool = False,
    intentionally_not_attempted: bool = False,
    unresolved_statement_count: int = 0,
) -> MFIContextStatus:
    """Resolve one public status from stable retrieval, classification, and QA data."""
    if intentionally_not_attempted:
        sources = list(retriever_statuses) or list(DEFAULT_CONTEXT_SOURCES)
        return not_attempted_context_status(sources)

    deduplicated = _deduplicated_documents(documents)
    source_counts = Counter(
        str(document.get("source") or "Unknown") for document in deduplicated
    )
    source_names = {
        *DEFAULT_CONTEXT_SOURCES,
        *(str(source) for source in retriever_statuses),
        *source_counts.keys(),
    }
    retrievers: dict[str, MFIContextRetrieverStatus] = {}
    for source in sorted(source_names, key=str.casefold):
        status = _retriever_state(retriever_statuses.get(source))
        count = int(source_counts.get(source, 0))
        if count and status in {"no_results", "not_attempted"}:
            status = "completed"
        retrievers[source] = MFIContextRetrieverStatus(
            status=status,
            retrieved_document_count=count,
        )

    total = len(deduplicated)
    classified = len(
        [statement for statement in statements if isinstance(statement, Mapping)]
    )
    accepted = len(accepted_context_statements(statements, deduplicated))
    failed_sources = any(item.status == "failed" for item in retrievers.values())

    if total == 0:
        overall = "retrieval_failed" if failed_sources else "no_results"
    elif classification_failed:
        overall = "classification_failed"
    elif accepted:
        overall = "available"
    else:
        overall = "no_accepted_statements"

    limitation_code = None
    if overall == "retrieval_failed":
        limitation_code = "context_retrieval_unavailable"
    elif overall == "classification_failed":
        limitation_code = "context_classification_unavailable"
    elif failed_sources:
        limitation_code = "context_partial_retrieval_unavailable"

    mode = extraction_mode if extraction_mode in _EXTRACTION_MODES else "fallback"
    if unresolved_statement_count:
        limitation_code = "context_partial_classification_unavailable"
    return MFIContextStatus(
        status=overall,
        retrievers=retrievers,
        total_deduplicated_documents_retrieved=total,
        statements_classified=classified,
        final_accepted_statements=accepted,
        extraction_mode=mode,
        limitation_code=limitation_code,
        classification_outcome="degraded" if unresolved_statement_count else "failed" if classification_failed else "completed" if total and mode == "llm" else "not_started",
        unresolved_statement_count=unresolved_statement_count,
    )
