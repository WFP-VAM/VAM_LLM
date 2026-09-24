from __future__ import annotations

import io

from docx import Document

from app.services.mfi_drafter.schemas import GenerateMFIReportOutput
from app.shared.docx_export import build_docx_bytes_from_report_blocks
from app.shared.report_blocks import build_mfi_report_blocks
import streamlit_shared as shared


def _flag(
    flag_id: str,
    *,
    severity: str = "medium",
    code: str = "review_required",
    claim_id: str | None = "dimension.price.finding.1",
    artifact_type: str = "dimension",
    artifact_id: str | None = "Price",
    field_name: str | None = "key_findings",
    message: str = "The claim requires review.",
    repairable: bool = True,
) -> dict:
    return {
        "flag_id": flag_id,
        "source": "deterministic",
        "code": code,
        "severity": severity,
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "field_name": field_name,
        "claim_id": claim_id,
        "message": message,
        "recommendation": "Review the affected field.",
        "metric_ids": [],
        "document_ids": [],
        "expected_value": None,
        "actual_value": None,
        "repairable": repairable,
    }


def _claim(text: str, *, substituted: bool = False) -> dict:
    return {
        "claim_id": "dimension.price.finding.1",
        "text": text,
        "claim_kind": "finding",
        "metric_ids": ["assessment.dimension.price.mean"],
        "document_ids": [],
        "scope": "assessment",
        "polarity": "neutral",
        "validation_status": "unverified",
        "validation_flags": [],
        "validation_flag_ids": [],
        "substituted": substituted,
    }


def _result(*, claim: dict | None = None, flags: list[dict] | None = None) -> dict:
    narrative = None
    if claim is not None:
        narrative = {
            "dimension": "Price",
            "is_priority": False,
            "summary": {
                **_claim("Price evidence is summarized below."),
                "claim_id": "dimension.price.summary",
                "validation_status": "verified",
            },
            "key_findings": [claim],
            "subdimension_analysis": [],
            "geographic_patterns": [],
            "data_limitations": [],
            "recommendations": [],
        }
    return {
        "country": "Testland",
        "data_collection_start": "2026-01-01",
        "data_collection_end": "2026-01-31",
        "methodology_version": "databridge-current",
        "score_authority": "synthetic_mock",
        "assessment_profile": {
            "priority_dimension_names": [],
            "priority_market_names": [],
            "markets": [],
            "limitations": [],
            "metric_ledger": {},
            "tables": {
                "dimension_rows": [],
                "regional_rows": [],
                "subsection_rows": [],
                "driver_rows": [],
                "relevant_item_rows": [],
                "priority_market_rows": [],
            },
        },
        "claim_catalog": {
            "assessment.dimension.price.mean": {
                "label": "Price mean",
                "formatted_value": "5.00/10",
                "scope": "assessment",
                "coverage_label": "3/3 assessed markets",
            }
        },
        "context_evidence": [],
        "dimension_narratives": {"Price": narrative} if narrative else {},
        "market_narratives": {},
        "executive_summary_narrative": {},
        "qa_review": {
            "status": "completed_with_warnings" if flags else "passed",
            "correction_attempts": 0,
            "correction_history": [],
            "flags": flags or [],
        },
        "generation_diagnostics": {
            "claim_substitutions": [],
            "unmatched_high_claim_ids": [],
        },
        "visualizations": {},
    }


def _docx_text(docx: bytes) -> str:
    document = Document(io.BytesIO(docx))
    parts = [paragraph.text for paragraph in document.paragraphs]
    parts.extend(cell.text for table in document.tables for row in table.rows for cell in row.cells)
    return "\n".join(parts)


def test_multiple_material_flags_share_one_adjacent_claim_warning() -> None:
    flags = [
        _flag("flag-a", code="scope_mismatch"),
        _flag("flag-b", code="polarity_mismatch"),
    ]
    blocks = build_mfi_report_blocks(_result(claim=_claim("Price scored 5.00/10."), flags=flags))

    claim_index = next(
        index
        for index, block in enumerate(blocks)
        if block.type == "paragraph"
        and (block.meta or {}).get("claim_id") == "dimension.price.finding.1"
    )
    warning = blocks[claim_index + 1]
    assert warning.type == "claim_warning"
    assert warning.meta["flag_codes"] == ["polarity_mismatch", "scope_mismatch"]
    assert warning.meta["disposition"] == "retained_unverified_for_delivery"
    assert sum(block.type == "claim_warning" for block in blocks) == 1

    qa_table = next(
        block
        for block in blocks
        if block.type == "table" and (block.meta or {}).get("title") == "QA findings"
    )
    assert [row["row_id"] for row in qa_table.meta["rows"]] == ["flag-b", "flag-a"]


def test_unverified_figure_is_retained_with_adjacent_warning_and_docx_notice() -> None:
    flag = _flag(
        "numeric-16",
        severity="high",
        code="numeric_value_mismatch",
        message="Numeric value 16 is not authorized by the cited evidence.",
    )
    flag["actual_value"] = "16"
    flag["delivery_disposition"] = (
        "retained_unverified_figure_for_delivery"
    )
    result = _result(
        claim=_claim("The review identified 16 affected markets."),
        flags=[flag],
    )
    result["qa_review"].update(
        {
            "status": "delivered_with_unverified_figures",
            "correction_attempts": 1,
            "unverified_figure_flag_ids": ["numeric-16"],
            "unverified_figure_claim_ids": ["dimension.price.finding.1"],
            "unverified_figure_values": ["16"],
            "correction_history": [
                {
                    "attempt_number": 1,
                    "artifact_type": "dimension",
                    "artifact_id": "Price",
                    "field_name": "key_findings",
                    "claim_id": "dimension.price.finding.1",
                    "flag_ids": ["numeric-16"],
                    "flag_codes": ["numeric_value_mismatch"],
                    "execution_outcome": "llm_completed",
                    "validation_outcome": "unresolved",
                }
            ],
        }
    )

    blocks = build_mfi_report_blocks(result)
    claim_index = next(
        index
        for index, block in enumerate(blocks)
        if block.type == "paragraph"
        and (block.meta or {}).get("claim_id") == "dimension.price.finding.1"
    )
    warning = blocks[claim_index + 1]
    assert warning.type == "claim_warning"
    assert warning.meta["disposition"] == (
        "retained_unverified_figure_for_delivery"
    )
    assert "FIGURE TO BE CHECKED" in warning.text
    assert "“16”" in warning.text
    assert "numeric_value_mismatch" in warning.text
    assert "Repair attempted: yes; attempts: 1" in warning.text

    qa_table = next(
        block
        for block in blocks
        if block.type == "table" and (block.meta or {}).get("title") == "QA findings"
    )
    assert qa_table.meta["rows"][0]["raw_values"]["disposition"] == (
        "retained_unverified_figure_for_delivery"
    )
    docx_text = _docx_text(
        build_docx_bytes_from_report_blocks(blocks, visualizations={})
    )
    assert "FIGURE TO BE CHECKED" in docx_text
    assert "Report delivered with figures requiring verification" in docx_text


def test_global_red_team_failure_is_listed_without_fake_claim_marker() -> None:
    flag = _flag(
        "system-red-team-execution-error",
        severity="medium",
        code="qa_execution_error",
        claim_id=None,
        artifact_type="global",
        artifact_id=None,
        field_name=None,
        message="The LLM Red-Team review could not be completed.",
        repairable=False,
    )
    blocks = build_mfi_report_blocks(_result(flags=[flag]))

    assert not [block for block in blocks if block.type == "claim_warning"]
    global_notice = next(
        block
        for block in blocks
        if block.type == "qa_warning" and (block.meta or {}).get("global_flag_ids")
    )
    assert global_notice.meta["global_flag_ids"] == [flag["flag_id"]]
    assert "qa_execution_error" in global_notice.text
    qa_table = next(
        block
        for block in blocks
        if block.type == "table" and (block.meta or {}).get("title") == "QA findings"
    )
    assert qa_table.meta["rows"][0]["raw_values"]["claim_id"] == ""
    assert (
        qa_table.meta["rows"][0]["raw_values"]["disposition"]
        == "global_unresolved"
    )


def test_low_advisory_does_not_create_claim_warning_or_material_table() -> None:
    flag = _flag("low-advisory", severity="low", code="style_advisory")
    result = _result(claim=_claim("Price scored 5.00/10."), flags=[flag])
    result["qa_review"]["status"] = "passed_with_advisories"

    blocks = build_mfi_report_blocks(result)

    assert not [block for block in blocks if block.type == "claim_warning"]
    assert not [
        block
        for block in blocks
        if block.type == "table" and (block.meta or {}).get("title") == "QA findings"
    ]


def test_streamlit_preview_renders_claim_warning_and_qa_table(monkeypatch) -> None:
    flag = _flag("preview-medium", code="scope_mismatch")
    blocks = build_mfi_report_blocks(
        _result(claim=_claim("Price scored 5.00/10."), flags=[flag])
    )
    events: list[tuple[str, object]] = []
    monkeypatch.setattr(shared.st, "warning", lambda value: events.append(("warning", value)))
    monkeypatch.setattr(shared.st, "markdown", lambda value: events.append(("markdown", value)))
    monkeypatch.setattr(
        shared.st,
        "dataframe",
        lambda value, **_kwargs: events.append(("dataframe", value)),
    )

    shared.render_report_blocks(
        [
            block.model_dump()
            for block in blocks
            if block.type in {"claim_warning", "table"}
            and (
                block.type == "claim_warning"
                or (block.meta or {}).get("title") == "QA findings"
            )
        ],
        {},
    )

    assert any(
        kind == "warning" and "dimension.price.finding.1" in str(value)
        for kind, value in events
    )
    assert any(kind == "dataframe" for kind, _value in events)


def test_public_schema_preserves_r4_audit_fields() -> None:
    definitions = GenerateMFIReportOutput.model_json_schema()["$defs"]

    assert "correction_history" in definitions["MFIQAReview"]["properties"]
    diagnostics = definitions["MFIGenerationDiagnostics"]["properties"]
    assert "claim_substitutions" in diagnostics
    assert "unmatched_high_claim_ids" in diagnostics
    assert "claim_identity_authority" in diagnostics
    assert "claim_identity_version" in diagnostics
    assert "ignored_model_identifier_count" in diagnostics
    assert "identity_fallback_artifacts" in diagnostics
    assert "delivery_contract_status" in diagnostics
