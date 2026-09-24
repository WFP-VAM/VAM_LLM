from __future__ import annotations


import pytest
from pydantic import ValidationError

from app.services.mfi_drafter.methodology import DISPLAY_DIMENSIONS
from app.services.mfi_drafter.schemas import MFIMarketNarrative
from app.shared.report_blocks import ReportBlock, build_mfi_report_blocks
import streamlit_shared as shared


def _claim(index: int, kind: str = "finding", scope: str = "assessment") -> dict:
    return {
        "claim_id": f"claim.{kind}.{index}",
        "text": f"Content-bearing {kind} {index}.",
        "claim_kind": kind,
        "metric_ids": [],
        "document_ids": [],
        "scope": scope,
        "polarity": "neutral",
        "validation_status": "verified",
        "validation_flags": [],
        "validation_flag_ids": [],
        "substituted": False,
    }


def _summary_claim(dimension: str) -> dict:
    claim = _claim(1, "summary")
    claim["claim_id"] = f"dimension.{dimension.casefold().replace(' ', '-')}.summary"
    claim["text"] = f"{dimension} evidence is summarized below."
    return claim


def _report_result() -> dict:
    dimensions = {
        dimension: {
            "dimension": dimension,
            "is_priority": dimension == "Price",
            "summary": _summary_claim(dimension),
            "key_findings": [],
            "subdimension_analysis": [],
            "geographic_patterns": [],
            "data_limitations": [],
            "recommendations": [],
        }
        for dimension in DISPLAY_DIMENSIONS
    }
    market_limitation = _claim(1, "limitation", "market")
    market_limitation["claim_id"] = "market.alpha.limitation.1"
    market_limitation["text"] = "Market-scoped explanatory evidence is incomplete."
    return {
        "country": "Testland",
        "data_collection_start": "2026-01-01",
        "data_collection_end": "2026-01-31",
        "methodology_version": "databridge-current",
        "score_authority": "synthetic_mock",
        "assessment_profile": {
            "priority_dimension_names": ["Price"],
            "priority_market_names": ["Alpha"],
            "markets": [
                {
                    "market_name": "Alpha",
                    "selection_order": 1,
                    "is_priority_market": True,
                }
            ],
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
        "claim_catalog": {},
        "context_status": {
            "status": "not_attempted",
            "limitation_code": None,
        },
        "context_evidence": [],
        "dimension_narratives": dimensions,
        "market_narratives": {
            "Alpha": {
                "market_name": "Alpha",
                "region": "North",
                "overall_mfi": 4.5,
                "score_rank": 1,
                "weak_dimensions": ["Price", "Service"],
                "priority_issues": [],
                "recommended_interventions": [],
                "limitations": [market_limitation],
                "modality_consideration": None,
            }
        },
        "executive_summary_narrative": {},
        "qa_review": {},
        "visualizations": {"dim_price_bars": "present"},
        "document_references": [],
    }


def test_market_limitation_model_is_additive_and_bounded() -> None:
    base = {
        "market_name": "Alpha",
        "overall_mfi": 4.5,
        "score_rank": 1,
    }
    assert MFIMarketNarrative(**base).limitations == []
    with pytest.raises(ValidationError):
        MFIMarketNarrative(
            **base,
            limitations=[_claim(1, "limitation", "market"), _claim(2, "limitation", "market")],
        )


def test_report_layout_is_typed_ordered_and_dataset_independent() -> None:
    blocks = build_mfi_report_blocks(_report_result())
    title = blocks[0]
    assert title.meta["mfi_layout"]["report_family"] == "mfi"
    assert title.meta["mfi_layout"]["country"] == "Testland"
    page_breaks = {
        block.text
        for block in blocks
        if (block.meta or {}).get("mfi_layout", {}).get("page_break_before")
    }
    assert page_breaks == {
        "Executive summary",
        "MFI dimensions",
        "Expanded priority-dimension evidence",
        "Lowest-scoring assessed markets selected for review",
        "Methodology, limitations, and QA notices",
    }
    for dimension in DISPLAY_DIMENSIONS:
        assert sum(
            block.type == "heading" and block.text == dimension for block in blocks
        ) == 1
    summary_index = next(
        index
        for index, block in enumerate(blocks)
        if (block.meta or {}).get("claim_id") == "dimension.price.summary"
    )
    figure_index = next(
        index for index, block in enumerate(blocks) if block.figure_id == "dim_price_bars"
    )
    assert summary_index < figure_index
    assert any(
        (block.meta or {}).get("claim_id") == "market.alpha.limitation.1"
        for block in blocks
    )
    # The single neutral methodology boundary remains allowed; no market narrative
    # renders a modality conclusion or compatibility-field payload.
    assert not any(
        (block.meta or {}).get("claim_id", "").endswith("modality_consideration")
        for block in blocks
    )


def test_streamlit_uses_mfi_major_section_dividers_without_reordering(monkeypatch) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(shared.st, "title", lambda text: events.append(("title", text)))
    monkeypatch.setattr(shared.st, "header", lambda text: events.append(("header", text)))
    monkeypatch.setattr(shared.st, "divider", lambda: events.append(("divider", "")))
    monkeypatch.setattr(shared.st, "markdown", lambda text: events.append(("text", text)))
    blocks = [
        ReportBlock(
            type="heading",
            text="MFI Report - Testland",
            level=1,
            meta={"mfi_layout": {"role": "title"}},
        ).model_dump(),
        ReportBlock(
            type="heading",
            text="Assessment metadata and coverage",
            level=2,
            meta={"mfi_layout": {"role": "major_section"}},
        ).model_dump(),
        ReportBlock(
            type="heading",
            text="Executive summary",
            level=2,
            meta={"mfi_layout": {"role": "major_section"}},
        ).model_dump(),
        ReportBlock(
            type="paragraph",
            text="Concise claim.",
            meta={"mfi_layout": {"role": "claim"}},
        ).model_dump(),
    ]
    shared.render_report_blocks(blocks, {})
    assert events == [
        ("title", "MFI Report - Testland"),
        ("header", "Assessment metadata and coverage"),
        ("divider", ""),
        ("header", "Executive summary"),
        ("text", "Concise claim."),
    ]
