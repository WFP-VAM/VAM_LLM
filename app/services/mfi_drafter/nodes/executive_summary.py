"""Node executive_summary: the executive summary and country context, drafted from the final sections."""
from __future__ import annotations
from ..evidence import evidence
from ..sections import texts


def synthesis(runtime, s):
    base = {**s["base"], **s["context"]}
    package = {"requested_sections": ["executive_summary", "country_context"], "EVIDENCE": evidence(base, "summary", []),
        "FINAL_DIMENSIONS": texts(s["final_dimensions"]), "FINAL_MARKETS": texts(s["final_markets"]),
        "FINAL_NOTES": [*s["final_dimensions"]["notes"], *s["final_markets"]["notes"]]}
    return {"summary": runtime.invoke("executive_summary", "executive_summary", package, ["executive_summary", "country_context"])}
