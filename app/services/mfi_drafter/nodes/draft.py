"""Nodes draft_dimensions and draft_markets: the first draft of a section family."""
from __future__ import annotations
from ..sections import generate_family


def draft_family(runtime, ledger, s, f, n):
    return {n: generate_family(runtime, ledger, s, f, n)}
