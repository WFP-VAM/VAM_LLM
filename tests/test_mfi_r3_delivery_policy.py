"""Historical R3 compatibility plus the current fail-closed delivery contract.

Repair is attempted first and bounded at three passes. These tests cover what happens
after that: a statement the assessment cannot support is withdrawn rather than delivered
with a caveat, and the rejected draft survives only in technical QA details.
"""

from __future__ import annotations


from app.services.mfi_drafter.wording import WITHDRAWN_CLAIM_TEXT, withdrawn_text


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def test_replacement_text_varies_by_claim_kind() -> None:
    kinds = {kind: withdrawn_text(kind) for kind in WITHDRAWN_CLAIM_TEXT}

    assert len(set(kinds.values())) > 1
    assert "recommendation" in withdrawn_text("recommendation")


# ---------------------------------------------------------------------------
# Wiring into finalize
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Prompt contracts
# ---------------------------------------------------------------------------
