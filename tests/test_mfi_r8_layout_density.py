from __future__ import annotations


from app.shared.report_blocks import ReportBlock
import streamlit_shared as shared


def test_streamlit_uses_mfi_major_section_dividers_without_reordering(monkeypatch) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(shared.st, "title", lambda text: events.append(("title", text)))
    monkeypatch.setattr(shared.st, "header", lambda text: events.append(("header", text)))
    monkeypatch.setattr(shared.st, "divider", lambda: events.append(("divider", "")))
    monkeypatch.setattr(shared.st, "markdown", lambda text: events.append(("text", text)))
    blocks = [
        ReportBlock(
            type="heading",
            text="MFI Report - Testland",
            level=1,
            meta={"mfi_layout": {"role": "title"}},
        ).model_dump(),
        ReportBlock(
            type="heading",
            text="Assessment metadata and coverage",
            level=2,
            meta={"mfi_layout": {"role": "major_section"}},
        ).model_dump(),
        ReportBlock(
            type="heading",
            text="Executive summary",
            level=2,
            meta={"mfi_layout": {"role": "major_section"}},
        ).model_dump(),
        ReportBlock(
            type="paragraph",
            text="Concise claim.",
            meta={"mfi_layout": {"role": "claim"}},
        ).model_dump(),
    ]
    shared.render_report_blocks(blocks, {})
    assert events == [
        ("title", "MFI Report - Testland"),
        ("header", "Assessment metadata and coverage"),
        ("divider", ""),
        ("header", "Executive summary"),
        ("text", "Concise claim."),
    ]
