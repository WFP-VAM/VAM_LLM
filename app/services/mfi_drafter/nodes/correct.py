"""Nodes correct_dimensions and correct_markets: the revised family when its review asks for it, else the draft."""
from __future__ import annotations
from ..sections import generate_family


def correction(runtime, ledger, s, f, n):
    result = generate_family(runtime, ledger, s, f, n) if s["review_"+f]["needs_revision"] else s["draft_"+f]
    ledger.change(lambda v: v.setdefault("light_review_outcomes", {}).setdefault(f, {}).update(
        correction="completed" if s["review_"+f]["needs_revision"] else "skipped"))
    return {"final_"+f: result}
