"""Node prepare_correction: the sections the next correction round redrafts."""
from __future__ import annotations

from ..state import MarketReportState
from ..qa import _correction_targets


def node_prepare_correction(state: MarketReportState) -> dict:
    """Capture material QA targets without clearing the flags that explain them."""
    targets = _correction_targets(state.get("skeptic_flags") or [])
    return {
        "correction_targets": targets,
        "correction_attempts": int(state.get("correction_attempts") or 0) + 1,
        "current_node": "prepare_correction",
    }
