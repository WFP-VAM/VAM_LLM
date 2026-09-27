"""Red-team QA and correction: the section vocabulary, the normalised flags, which sections a correction targets, and
the QA review a run records."""
from __future__ import annotations

from typing import List, Dict, Any, Optional, Mapping

from .prompts import (
    _json_for_prompt,
)


QA_CORE_SECTIONS = {
    "HIGHLIGHTS",
    "MARKET_OVERVIEW",
    "COMMODITY_ANALYSIS",
    "REGIONAL_HIGHLIGHTS",
}


QA_MODULE_SECTIONS = {
    "EXCHANGE_RATE_ANALYSIS": "exchange_rate",
    "FUEL_ENERGY_ANALYSIS": "fuel_energy",
    "LIVESTOCK_ANIMAL_PRODUCTS_ANALYSIS": "livestock_animal_products",
    "LABOUR_MARKET_ANALYSIS": "labour_market",
}


QA_SECTION_IDS = QA_CORE_SECTIONS | set(QA_MODULE_SECTIONS) | {"GLOBAL"}


QA_MATERIAL_SEVERITIES = {"high", "medium"}


def _normalized_qa_flags(value: Any) -> List[Dict[str, Any]]:
    flags: List[Dict[str, Any]] = []
    for raw in value or []:
        if not isinstance(raw, Mapping):
            continue
        section = str(raw.get("section") or "GLOBAL").strip().upper()
        if section not in QA_SECTION_IDS:
            section = "GLOBAL"
        severity = str(raw.get("severity") or "medium").strip().lower()
        if severity not in {"high", "medium", "low"}:
            severity = "medium"
        flags.append(
            {
                "section": section,
                "claim": str(raw.get("claim") or ""),
                "issue_type": str(raw.get("issue_type") or "unsupported_speculation"),
                "severity": severity,
                "details": str(raw.get("details") or ""),
                "recommendation": str(raw.get("recommendation") or ""),
            }
        )
    return flags


def _material_qa_flags(value: Any) -> List[Dict[str, Any]]:
    return [flag for flag in _normalized_qa_flags(value) if flag["severity"] in QA_MATERIAL_SEVERITIES]


def _correction_targets(value: Any) -> List[str]:
    material = _material_qa_flags(value)
    if not material:
        return []
    if any(flag["section"] == "GLOBAL" for flag in material):
        return ["GLOBAL"]
    ordered = [
        "EXCHANGE_RATE_ANALYSIS",
        "FUEL_ENERGY_ANALYSIS",
        "LIVESTOCK_ANIMAL_PRODUCTS_ANALYSIS",
        "LABOUR_MARKET_ANALYSIS",
        "HIGHLIGHTS",
        "MARKET_OVERVIEW",
        "COMMODITY_ANALYSIS",
        "REGIONAL_HIGHLIGHTS",
    ]
    selected = {flag["section"] for flag in material}
    return [section for section in ordered if section in selected]


def _targeted(state: Mapping[str, Any], section: str) -> bool:
    targets = set(state.get("correction_targets") or [])
    return not targets or "GLOBAL" in targets or section in targets


def _correction_flags_json(state: Mapping[str, Any], section: Optional[str] = None) -> str:
    flags = _normalized_qa_flags(state.get("skeptic_flags") or [])
    if section and "GLOBAL" not in set(state.get("correction_targets") or []):
        flags = [flag for flag in flags if flag["section"] in {section, "GLOBAL"}]
    return _json_for_prompt(flags)


def qa_review_from_state(state: Mapping[str, Any], *, recorded: bool = True) -> Dict[str, Any]:
    if not recorded:
        return {"status": "not_recorded", "correction_attempts": 0, "flags": []}
    flags = _normalized_qa_flags(state.get("skeptic_flags") or [])
    material = [flag for flag in flags if flag["severity"] in QA_MATERIAL_SEVERITIES]
    if material:
        status = "completed_with_warnings"
    elif flags:
        status = "passed_with_advisories"
    else:
        status = "passed"
    return {
        "status": status,
        "correction_attempts": int(state.get("correction_attempts") or 0),
        "flags": flags,
    }


def normalize_qa_review(result: Mapping[str, Any]) -> Dict[str, Any]:
    existing = result.get("qa_review")
    if not isinstance(existing, Mapping):
        return {"status": "not_recorded", "correction_attempts": 0, "flags": []}
    status = str(existing.get("status") or "not_recorded")
    allowed = {"passed", "passed_with_advisories", "completed_with_warnings", "not_recorded"}
    if status not in allowed:
        status = "not_recorded"
    return {
        "status": status,
        "correction_attempts": int(existing.get("correction_attempts") or 0),
        "flags": _normalized_qa_flags(existing.get("flags") or []),
    }
