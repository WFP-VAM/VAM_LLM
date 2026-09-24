from __future__ import annotations

from pathlib import Path


def test_report_graph_and_ui_have_no_internal_legacy_reads():
    root = Path(__file__).resolve().parents[1]
    paths = [
        root / "app/services/mfi_drafter/light_graph.py",
        root / "app/shared/report_blocks.py",
        root / "pages/4_MFI_Drafter.py",
    ]
    forbidden = ('"sub_scores"', '"risk_distribution"', '"risk_level"')
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{token} is read internally by {path.name}"
