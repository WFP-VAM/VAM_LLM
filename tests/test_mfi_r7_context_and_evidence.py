from __future__ import annotations


import pytest
from pydantic import ValidationError

from app.services.mfi_drafter.context_status import resolve_context_status
from app.services.mfi_drafter.schemas import (
    MFIContextRetrieverStatus,
    MFIContextStatus,
)


def _document(doc_id: str = "doc-1", source: str = "ReliefWeb") -> dict:
    return {
        "doc_id": doc_id,
        "source": source,
        "title": f"Document {doc_id}",
        "url": f"https://example.test/{doc_id}",
        "date": "2026-01-15",
        "content": "Contextual source text.",
    }


def _statement(
    statement_id: str = "context.statement.1",
    *,
    document_ids: list[str] | None = None,
    classification: str = "corroborating",
    substituted: bool = False,
) -> dict:
    return {
        "statement_id": statement_id,
        "text": "The cited document reports a relevant market condition.",
        "classification": classification,
        "document_ids": document_ids if document_ids is not None else ["doc-1"],
        "validation_status": "verified",
        "validation_flags": [],
        "substituted": substituted,
    }


def test_context_status_models_are_frozen_sorted_and_count_consistent() -> None:
    status = MFIContextStatus(
        status="available",
        retrievers={
            "Seerist": MFIContextRetrieverStatus(
                status="no_results", retrieved_document_count=0
            ),
            "ReliefWeb": MFIContextRetrieverStatus(
                status="completed", retrieved_document_count=1
            ),
        },
        total_deduplicated_documents_retrieved=1,
        statements_classified=1,
        final_accepted_statements=1,
        extraction_mode="llm",
    )
    assert list(status.retrievers) == ["ReliefWeb", "Seerist"]
    with pytest.raises(ValidationError):
        status.status = "no_results"
    with pytest.raises(ValidationError):
        MFIContextStatus(
            status="no_results",
            retrievers={
                "ReliefWeb": MFIContextRetrieverStatus(
                    status="completed", retrieved_document_count=1
                )
            },
            total_deduplicated_documents_retrieved=0,
            statements_classified=0,
            final_accepted_statements=0,
        )


@pytest.mark.parametrize(
    ("retrievers", "documents", "statements", "failed", "expected", "code"),
    [
        (
            {"ReliefWeb": "completed", "Seerist": "no_results"},
            [_document()],
            [_statement()],
            False,
            "available",
            None,
        ),
        (
            {"ReliefWeb": "no_results", "Seerist": "no_results"},
            [],
            [],
            False,
            "no_results",
            None,
        ),
        (
            {"ReliefWeb": "failed", "Seerist": "no_results"},
            [],
            [],
            False,
            "retrieval_failed",
            "context_retrieval_unavailable",
        ),
        (
            {"ReliefWeb": "completed", "Seerist": "no_results"},
            [_document()],
            [],
            True,
            "classification_failed",
            "context_classification_unavailable",
        ),
        (
            {"ReliefWeb": "completed", "Seerist": "no_results"},
            [_document()],
            [_statement(classification="unrelated")],
            False,
            "no_accepted_statements",
            None,
        ),
        (
            {"ReliefWeb": "completed", "Seerist": "failed"},
            [_document()],
            [_statement()],
            False,
            "available",
            "context_partial_retrieval_unavailable",
        ),
    ],
)
def test_context_state_resolution(
    retrievers, documents, statements, failed, expected, code
) -> None:
    status = resolve_context_status(
        retriever_statuses=retrievers,
        documents=documents,
        statements=statements,
        extraction_mode="fallback" if failed else "llm",
        classification_failed=failed,
    )
    assert status.status == expected
    assert status.limitation_code == code
