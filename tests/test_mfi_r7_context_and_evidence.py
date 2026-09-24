from __future__ import annotations


import pytest
from pydantic import ValidationError

from app.services.mfi_drafter.context_status import (
    not_attempted_context_status,
    reconcile_context_status,
    resolve_context_status,
)
from app.services.mfi_drafter.evidence_notes import compose_evidence_note
from app.services.mfi_drafter.schemas import (
    MFIContextRetrieverStatus,
    MFIContextStatus,
)
from app.shared.report_blocks import build_mfi_report_blocks


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


def _empty_profile() -> dict:
    return {
        "tables": {
            "dimension_rows": [],
            "regional_rows": [],
            "subsection_rows": [],
            "driver_rows": [],
            "relevant_item_rows": [],
            "priority_market_rows": [],
        },
        "metric_ledger": {},
        "priority_dimension_names": [],
        "markets": [],
        "limitations": [],
    }


def _report_result(*, context_status: dict, evidence: list[dict], docs: list[dict]) -> dict:
    return {
        "country": "Testland",
        "data_collection_start": "2026-01-01",
        "data_collection_end": "2026-01-31",
        "assessment_profile": _empty_profile(),
        "claim_catalog": {},
        "context_status": context_status,
        "context_evidence": evidence,
        "contextual_documents": docs,
        "document_references": [
            {key: document.get(key) for key in ("doc_id", "source", "title", "url", "date")}
            for document in docs
        ],
        "dimension_narratives": {},
        "market_narratives": {},
        "executive_summary_narrative": {},
        "visualizations": {},
        "qa_review": {},
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


def test_offline_context_is_not_attempted_and_qa_withdrawal_removes_acceptance() -> None:
    offline = not_attempted_context_status()
    assert offline.status == "not_attempted"
    assert {item.status for item in offline.retrievers.values()} == {"not_attempted"}

    available = resolve_context_status(
        retriever_statuses={"ReliefWeb": "completed", "Seerist": "no_results"},
        documents=[_document()],
        statements=[_statement()],
        extraction_mode="llm",
    )
    reconciled = reconcile_context_status(
        available,
        documents=[_document()],
        statements=[_statement(substituted=True, document_ids=[])],
    )
    assert reconciled.status == "no_accepted_statements"
    assert reconciled.final_accepted_statements == 0


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            "no_results",
            "No contextual documents were retrieved for the selected country and period.",
        ),
        (
            "retrieval_failed",
            "Context retrieval was unavailable; interpretation relies only on the MFI assessment.",
        ),
        (
            "classification_failed",
            "Context classification was unavailable after documents were retrieved; interpretation relies only on the MFI assessment.",
        ),
        (
            "no_accepted_statements",
            "Documents were retrieved, but none met the evidence-classification requirements.",
        ),
        (
            "not_attempted",
            "Context retrieval was not run for this report; interpretation relies only on the MFI assessment.",
        ),
    ],
)
def test_context_section_always_discloses_nonavailable_state(status, message) -> None:
    limitation = {
        "retrieval_failed": "context_retrieval_unavailable",
        "classification_failed": "context_classification_unavailable",
    }.get(status)
    blocks = build_mfi_report_blocks(
        _report_result(
            context_status={"status": status, "limitation_code": limitation},
            evidence=[],
            docs=[_document()] if status in {"classification_failed", "no_accepted_statements"} else [],
        )
    )
    context_index = next(
        index
        for index, block in enumerate(blocks)
        if block.type == "heading" and block.text == "Context and sources"
    )
    assert blocks[context_index + 1].text == message
    if limitation:
        assert blocks[context_index + 2].type == "limitation_box"
        assert blocks[context_index + 2].meta["code"] == limitation


def test_available_context_renders_only_accepted_statements_and_cited_references() -> None:
    docs = [_document("doc-1"), _document("doc-2", "Seerist")]
    evidence = [
        _statement("accepted", document_ids=["doc-1"]),
        _statement("unrelated", document_ids=["doc-2"], classification="unrelated"),
        _statement("uncited", document_ids=[]),
    ]
    status = resolve_context_status(
        retriever_statuses={"ReliefWeb": "completed", "Seerist": "completed"},
        documents=docs,
        statements=evidence,
        extraction_mode="llm",
    )
    blocks = build_mfi_report_blocks(
        _report_result(
            context_status=status.model_dump(), evidence=evidence, docs=docs
        )
    )
    visible_claim_ids = {
        block.meta.get("claim_id")
        for block in blocks
        if block.type == "paragraph" and isinstance(block.meta, dict)
    }
    assert "accepted" in visible_claim_ids
    assert "unrelated" not in visible_claim_ids
    assert "uncited" not in visible_claim_ids
    reference_block = next(block for block in blocks if block.type == "references")
    assert [item["doc_id"] for item in reference_block.references] == ["doc-1"]


def test_partial_retrieval_keeps_valid_context_and_adds_stable_limitation() -> None:
    document = _document()
    evidence = [_statement()]
    status = resolve_context_status(
        retriever_statuses={"ReliefWeb": "completed", "Seerist": "failed"},
        documents=[document],
        statements=evidence,
        extraction_mode="llm",
    )
    blocks = build_mfi_report_blocks(
        _report_result(
            context_status=status.model_dump(), evidence=evidence, docs=[document]
        )
    )
    assert any(
        block.type == "limitation_box"
        and block.meta.get("code") == "context_partial_retrieval_unavailable"
        for block in blocks
    )
    assert any(
        block.type == "paragraph"
        and isinstance(block.meta, dict)
        and block.meta.get("claim_id") == "context.statement.1"
        for block in blocks
    )


@pytest.mark.parametrize(
    ("scope", "entry", "expected"),
    [
        ("assessment", {}, "Claim scope: assessed-market profile"),
        ("region", {"region": "North"}, "Claim scope: region — North"),
        ("market", {"market_name": "Alpha"}, "Claim scope: market — Alpha"),
        ("surveyed_traders", {}, "Claim scope: surveyed traders"),
    ],
)
def test_evidence_note_scope_labels(scope, entry, expected) -> None:
    catalog_entry = {
        "label": "Metric",
        "formatted_value": "5.00/10",
        "scope": scope,
        "representation_kind": "fixed_metric",
        "representation_complete": True,
        "representation_required": False,
        **entry,
    }
    note = compose_evidence_note(
        {"scope": scope, "metric_ids": ["metric"], "document_ids": []},
        {"metric": catalog_entry},
        {},
    )
    assert note.startswith(expected)


def test_item_applicability_and_document_only_notes_are_always_disclosed() -> None:
    catalog = {
        "item": {
            "label": "Barley: unfavorable rate",
            "formatted_value": "40.0%",
            "scope": "assessment",
            "representation_kind": "item",
            "representation_complete": False,
            "representation_required": True,
            "represented_market_count": 12,
            "assessed_market_count": 27,
        },
        "quality": {
            "label": "Acceptable food quality",
            "formatted_value": "75.0%",
            "scope": "assessment",
            "representation_kind": "applicability",
            "representation_complete": False,
            "representation_required": True,
            "represented_market_count": 8,
            "assessed_market_count": 27,
        },
    }
    note = compose_evidence_note(
        {"metric_ids": ["item", "quality"], "document_ids": []}, catalog, {}
    )
    assert "Item representation: Barley observed in 12/27 assessed markets" in note
    assert "Applicability representation:" in note
    document_only = compose_evidence_note(
        {"metric_ids": [], "document_ids": ["doc-1"]}, {}, {"doc-1": _document()}
    )
    assert document_only.startswith("Claim scope: context;")
