"""Phase R3 tests for narrative safety rules.

R3 stops the report reaching operational conclusions the assessment cannot support. The
rules are deliberately broad — a hedged conclusion is still a conclusion — which makes
false positives the main hazard: the entire deterministic path must keep validating
cleanly, or several test files go red at once.

The first test in this file is therefore the most important one. It runs every string the
system produces without an LLM through every new rule and asserts none of them fires.
"""

from __future__ import annotations

import re

import pytest

from app.services.mfi_drafter import wording
from app.services.mfi_drafter.methodology import DIMENSION_DESCRIPTIONS


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_neutral_statement_is_permitted_by_the_negation_rule() -> None:
    """The statement R3 introduces must not read as the defect it replaces.

    It names a modality and reaches a conclusion about scope, so only the explicit
    negation keeps it legal.
    """
    assert wording.modality_conclusions(wording.NEUTRAL_SCOPE_STATEMENT) == []
    assert re.search(
        wording.METHODOLOGICAL_NEGATION_PATTERN,
        wording.NEUTRAL_SCOPE_STATEMENT,
        re.IGNORECASE,
    )


def test_price_methodology_description_is_permitted() -> None:
    """This sentence is injected into the Price prompt, so the model is taught to echo it.

    Before R3 the Price-gated affordability check flagged that echo, which is a live false
    positive the negation rule removes.
    """
    description = DIMENSION_DESCRIPTIONS["Price"]

    assert "affordability" in description
    assert wording.affordability_claims(description) == []


# ---------------------------------------------------------------------------
# Modality detection
# ---------------------------------------------------------------------------


MODALITY_SUBJECTS = ["cash", "vouchers", "CVA", "CBT", "in-kind support", "a hybrid approach"]
MODALITY_VERDICTS = [
    "is feasible",
    "could be viable",
    "would be appropriate",
    "may be constrained",
    "could be compromised",
    "may undermine the response",
    "poses significant risks",
]


@pytest.mark.parametrize("subject", MODALITY_SUBJECTS)
@pytest.mark.parametrize("verdict", MODALITY_VERDICTS)
def test_modality_verdicts_are_detected_in_every_combination(subject, verdict) -> None:
    """A hedged conclusion is still a conclusion — conditional modals do not exempt."""
    text = f"Based on the assessment, {subject} {verdict}."

    assert wording.modality_conclusions(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Cash and voucher assistance are used widely in this context.",
        "The market accepts multiple payment types.",
        "Further verification of the cited evidence is recommended.",
        "Traders reported stock shortages during the assessment period.",
    ],
)
def test_naming_a_modality_without_a_verdict_is_permitted(text) -> None:
    assert wording.modality_conclusions(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "The service environment is constrained.",
        "Assortment depth may be compromised at several assessed markets.",
    ],
)
def test_a_verdict_without_a_modality_is_permitted(text) -> None:
    assert wording.modality_conclusions(text) == []


def test_a_verdict_in_another_sentence_is_not_a_conclusion() -> None:
    """Conjunction is evaluated per sentence, not per claim."""
    text = "Cash and vouchers are both in use. The road surface is compromised."

    assert wording.modality_conclusions(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "MFI evidence cannot determine transfer modality.",
        "The Price dimension does not by itself measure purchasing power.",
        "This assessment does not establish whether cash is appropriate.",
    ],
)
def test_methodological_negation_is_permitted(text) -> None:
    assert wording.modality_conclusions(text) == []
    assert wording.affordability_claims(text) == []


def test_the_observed_report_sentences_are_caught() -> None:
    """The exact wording the diagnostic review found in the delivered report."""
    observed = [
        "Given the market's severe functional limitations, the effectiveness of an "
        "exclusively cash or voucher-based response could be compromised.",
        "Given the market's functional capacity to accept multiple forms of payment, "
        "both cash and voucher assistance could be viable.",
        "While the acceptance of multiple payment types is conducive to cash and voucher "
        "assistance, the market's severe functional limitations warrant caution.",
    ]

    for text in observed:
        assert wording.modality_conclusions(text), text


# ---------------------------------------------------------------------------
# Affordability and causality
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Price instability may erode the purchasing power of transfers.",
        "Goods have become unaffordable for many households.",
        "Rising inflation was observed across assessed markets.",
        "Beneficiaries may struggle to obtain essential items.",
    ],
)
def test_affordability_inference_is_detected(text) -> None:
    assert wording.affordability_claims(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Injecting purchasing power without addressing supply could exacerbate shortages.",
        "Price instability may erode the value of assistance.",
        "The shortage resulted in reduced assortment depth.",
        "Weak infrastructure led to longer replenishment times.",
    ],
)
def test_causal_assertion_is_detected(text) -> None:
    assert wording.causal_claims(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Service scored lowest among the nine assessment dimensions.",
        "The unweighted mean across assessed markets was reported below.",
        "Review the cited evidence with local teams.",
    ],
)
def test_descriptive_text_is_not_causal(text) -> None:
    assert wording.causal_claims(text) == []


# ---------------------------------------------------------------------------
# Population wording
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Here 35.1% of traders reported scarcity.",
        "Instability affected 72.8% of responses.",
        "Fully 100.0% of surveyed traders lacked receipts.",
        "In this market 93.3% of vendors reported delays.",
    ],
)
def test_respondent_attribution_is_detected(text) -> None:
    assert wording.pooled_population_phrases(text), text


@pytest.mark.parametrize(
    "text",
    [
        "The unweighted mean market-level unfavorable rate was 35.1%.",
        "The condition was unfavorable in 40.7% of assessed markets.",
        "The unweighted mean of market-level trader proportions was 64.9%.",
    ],
)
def test_approved_aggregation_wording_is_permitted(text) -> None:
    assert wording.pooled_population_phrases(text) == []
    assert wording.has_approved_aggregation_wording(text)


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("A rate of `35.1%` was reported.", "A rate of 35.1% was reported."),
        ("The score was **3.33/10** overall.", "The score was 3.33/10 overall."),
        ("*Emphasis* around text.", "Emphasis around text."),
        ("# Heading text", "Heading text"),
        ("- A bullet item", "A bullet item"),
        ("See [the report](https://example.org).", "See the report."),
        ("```\ncode block\n```", "code block"),
        ("Visit <https://example.org> today.", "Visit https://example.org today."),
    ],
)
def test_sanitizer_removes_delimiters(raw, expected) -> None:
    assert wording.sanitize_claim_text(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "1. The first ranked market.",
        "Coverage for cereal_food was partial.",
        "Access & Protection scored 9.85/10.",
        "The rate was 35.1% across 27 assessed markets.",
    ],
)
def test_sanitizer_leaves_safe_text_untouched(raw) -> None:
    """Ordered-list markers and underscores are load-bearing, not markup."""
    assert wording.sanitize_claim_text(raw) == raw


def test_sanitizer_aborts_rather_than_alter_a_number() -> None:
    """A link whose URL carries digits is returned raw for the validator to flag."""
    raw = "See [the rate](https://example.org/2024)."

    cleaned = wording.sanitize_claim_text(raw)

    assert cleaned == raw.strip()
    assert "markdown_link" in wording.residual_markup(cleaned)


def test_sanitized_text_reports_no_residual_markup() -> None:
    raw = "The `value` was **3.33/10** per [source](https://example.org)."

    assert wording.residual_markup(wording.sanitize_claim_text(raw)) == ()


# ---------------------------------------------------------------------------
# Shared-definition contract
# ---------------------------------------------------------------------------
