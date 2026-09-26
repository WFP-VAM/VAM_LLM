"""Nodes review_dimensions and review_markets: the review of a family's draft, read with the other draft."""
from __future__ import annotations
from ..sections import generate_family


def review_family(runtime, ledger, s, f, n):
    return {n: generate_family(runtime, ledger, s, f, n)}
