from __future__ import annotations

from app.services.mfi_drafter.identity import context_token


def test_market_context_tokens_prevent_slug_case_and_unicode_collisions() -> None:
    names = ["Cafe", "Café", "CAFE", "cafe"]
    tokens = [context_token(name) for name in names]
    assert len(tokens) == len(set(tokens))
    assert all(token.startswith("cafe_") for token in tokens)
