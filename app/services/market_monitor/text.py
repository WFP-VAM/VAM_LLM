"""Text helpers shared by the nodes and the optional modules: generated prose normalised to the report language,
localised numbers and percentages, and short texts without duplicates."""
from __future__ import annotations

from typing import List, Dict, Any

from .i18n import (
    format_decimal_value,
    format_percent_value,
    normalize_generated_text,
)
from .state import _state_language


def _normalize_output_text(text: Any, state: Dict[str, Any]) -> tuple[str, List[str]]:
    refs = state.get("document_references") or []
    titles = [str(ref.get("title") or "") for ref in refs if isinstance(ref, dict)]
    return normalize_generated_text(text, _state_language(state), reference_titles=titles)


def _validated_prose(text: str, state: Dict[str, Any]) -> str:
    normalized, _warnings = _normalize_output_text(text, state)
    if not normalized.strip():
        raise ValueError("LLM prose is empty after normalization")
    return normalized


def _plain_or_localized_number(value: Any, language: str, *, decimals: int = 1) -> str:
    if language == "en":
        return str(value)
    return format_decimal_value(value, language, decimals=decimals)


def format_pct(value, language: str = "en") -> str:
    """Format percentage deltas with direction arrows."""
    return format_percent_value(value, language, include_arrow=True)


def _dedupe_text(items: List[Any]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for item in items or []:
        text_value = str(item or "").strip()
        if not text_value:
            continue
        key = text_value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text_value)
    return out
