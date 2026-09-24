from __future__ import annotations


import pytest

from app.services.mfi_drafter.claim_identity import (
    CLAIM_IDENTITY_AUTHORITY,
    CLAIM_IDENTITY_VERSION,
    canonical_claim_index,
    canonicalize_narrative_identities,
    context_statement_id,
    context_token,
    dimension_claim_id,
    executive_claim_id,
    market_claim_id,
    subdimension_claim_id,
)
from app.shared.report_blocks import ReportBlock, resolve_mfi_report_blocks


def _model_claim(text: str, *, scope: str = "assessment") -> dict:
    return {
        "claim_id": "stable id",
        "text": text,
        "claim_kind": "finding",
        "metric_ids": [],
        "document_ids": [],
        "scope": scope,
        "polarity": "neutral",
    }


def test_identity_builders_cover_every_canonical_artifact_location() -> None:
    assert CLAIM_IDENTITY_AUTHORITY == "application"
    assert CLAIM_IDENTITY_VERSION == "mfi-claim-id-v2"
    assert dimension_claim_id("Food Quality", "summary", 1) == (
        "dimension.food_quality.summary.1"
    )
    assert subdimension_claim_id("Food Quality", 2) == (
        "dimension.food_quality.subdimension.2.interpretation"
    )
    assert executive_claim_id("scope_statement", 1) == (
        "executive.scope_statement.1"
    )
    assert context_statement_id(3) == "context.statement.3"
    assert market_claim_id("Café", "issue", 1).startswith(
        f"market.{context_token('Café')}.issue."
    )


def test_market_context_tokens_prevent_slug_case_and_unicode_collisions() -> None:
    names = ["Cafe", "Café", "CAFE", "cafe"]
    tokens = [context_token(name) for name in names]
    assert len(tokens) == len(set(tokens))
    assert all(token.startswith("cafe_") for token in tokens)


@pytest.mark.parametrize("market_name", ["Dangbo", "Café"])
def test_market_limitations_use_the_canonical_singular_identity_token(
    market_name: str,
) -> None:
    market = {
        "market_name": market_name,
        "region": "Region A",
        "overall_mfi": 4.5,
        "score_rank": 1,
        "weak_dimensions": ["Price"],
        "priority_issues": [_model_claim("Issue.", scope="market")],
        "recommended_interventions": [
            _model_claim("Intervention.", scope="market")
        ],
        "limitations": [
            {
                **_model_claim("Market-specific limitation.", scope="market"),
                "metric_ids": ["market.coverage"],
            }
        ],
        "modality_consideration": None,
    }
    _dimensions, markets, _executive, _context = (
        canonicalize_narrative_identities(
            dimension_narratives={},
            market_narratives={market_name: market},
            executive_narrative={},
        )
    )
    limitation_id = markets[market_name]["limitations"][0]["claim_id"]
    assert limitation_id == market_claim_id(market_name, "limitation", 1)
    assert ".limitations." not in limitation_id
    index = canonical_claim_index({}, markets, {}, [])
    assert limitation_id in index


def test_resolver_prefers_persisted_validated_blocks(monkeypatch) -> None:
    stored = [ReportBlock(type="heading", text="Validated", level=1).model_dump()]

    def fail_if_rebuilt(_result):
        raise AssertionError("persisted blocks were rebuilt")

    monkeypatch.setattr(
        "app.shared.report_blocks.build_mfi_report_blocks", fail_if_rebuilt
    )
    resolved = resolve_mfi_report_blocks({"report_blocks": stored})
    assert [block.text for block in resolved] == ["Validated"]
