from __future__ import annotations

import base64
import io
import re
from typing import Any, Dict, List, Optional

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Inches, Mm, Pt, RGBColor

from app.services.market_monitor.i18n import t

from .report_blocks import ReportBlock, basket_definition_table_display


_MFI_STYLE_NAMES = {
    "title": "MFI Title",
    "major_section": "MFI Major Heading",
    "subsection": "MFI Subsection Heading",
    "minor_heading": "MFI Minor Heading",
    "body": "MFI Body",
    "claim": "MFI Claim",
    "evidence_note": "MFI Evidence Note",
    "definition": "MFI Definition",
    "notice": "MFI Notice",
    "caption": "MFI Caption",
    "header": "MFI Header",
    "footer": "MFI Footer",
    "table_header": "MFI Table Header",
    "table_body": "MFI Table Body",
}


def _mfi_layout(block: ReportBlock) -> Dict[str, Any]:
    meta = block.meta or {}
    layout = meta.get("mfi_layout") if isinstance(meta, dict) else None
    return dict(layout) if isinstance(layout, dict) else {}


def _is_mfi_document(blocks: List[ReportBlock]) -> bool:
    return any(
        _mfi_layout(block).get("report_family") == "mfi"
        or (
            isinstance(block.meta, dict)
            and str(block.meta.get("table_kind") or "").startswith("mfi_")
        )
        for block in blocks
    )


def _set_style_font(style: Any, *, name: str, size: float, color: str) -> None:
    style.font.name = name
    style.font.size = Pt(size)
    style.font.color.rgb = RGBColor.from_string(color)
    style._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:ascii"), name)
    style._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:hAnsi"), name)
    style._element.get_or_add_rPr().get_or_add_rFonts().set(qn("w:eastAsia"), name)


def _ensure_mfi_style(
    doc: Document,
    role: str,
    *,
    size: float,
    color: str = "000000",
    bold: bool = False,
    italic: bool = False,
    before: float = 0.0,
    after: float = 0.0,
    line_spacing: float = 1.0,
    keep_with_next: bool = False,
    keep_together: bool = False,
    outline_level: Optional[int] = None,
) -> Any:
    name = _MFI_STYLE_NAMES[role]
    try:
        style = doc.styles[name]
    except KeyError:
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
    _set_style_font(style, name="Arial", size=size, color=color)
    style.font.bold = bold
    style.font.italic = italic
    fmt = style.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line_spacing
    fmt.keep_with_next = keep_with_next
    fmt.keep_together = keep_together
    if outline_level is not None:
        properties = style._element.get_or_add_pPr()
        existing = properties.find(qn("w:outlineLvl"))
        if existing is None:
            existing = OxmlElement("w:outlineLvl")
            properties.append(existing)
        existing.set(qn("w:val"), str(int(outline_level)))
    return style


def _configure_mfi_styles(doc: Document) -> None:
    _ensure_mfi_style(doc, "title", size=20, color="0072BC", bold=True, after=6, keep_with_next=True, outline_level=0)
    _ensure_mfi_style(doc, "major_section", size=14, color="0072BC", bold=True, before=12, after=6, keep_with_next=True, outline_level=1)
    _ensure_mfi_style(doc, "subsection", size=11, color="1F4D78", bold=True, before=8, after=4, keep_with_next=True, outline_level=2)
    _ensure_mfi_style(doc, "minor_heading", size=9.5, color="1F4D78", bold=True, before=6, after=3, keep_with_next=True, outline_level=3)
    _ensure_mfi_style(doc, "body", size=9, after=3, line_spacing=1.05)
    _ensure_mfi_style(doc, "claim", size=9, after=2, line_spacing=1.05, keep_together=True)
    _ensure_mfi_style(doc, "evidence_note", size=8, color="505050", italic=True, after=2, keep_together=True)
    _ensure_mfi_style(doc, "definition", size=8.5, after=2, line_spacing=1.05, keep_with_next=True, keep_together=True)
    _ensure_mfi_style(doc, "notice", size=8.5, after=2, line_spacing=1.05, keep_together=True)
    _ensure_mfi_style(doc, "caption", size=8, color="505050", italic=True, after=4, keep_together=True)
    _ensure_mfi_style(doc, "header", size=8, color="666666", after=0)
    _ensure_mfi_style(doc, "footer", size=8, color="666666", after=0)
    _ensure_mfi_style(doc, "table_header", size=7, color="FFFFFF", bold=True, after=0, keep_together=True)
    _ensure_mfi_style(doc, "table_body", size=7, after=0, line_spacing=1.0, keep_together=True)


def _append_word_field(paragraph: Any, instruction: str, placeholder: str = "1") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f" {instruction} "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = placeholder
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instr, separate, text, end):
        run._r.append(element)


def _configure_mfi_document(doc: Document, context: Dict[str, Any]) -> None:
    _configure_mfi_styles(doc)
    country = str(context.get("country") or "").strip()
    for section in doc.sections:
        section.page_width = Mm(210)
        section.page_height = Mm(297)
        section.top_margin = Inches(0.7)
        section.right_margin = Inches(0.7)
        section.bottom_margin = Inches(0.7)
        section.left_margin = Inches(0.7)
        section.header_distance = Inches(0.3)
        section.footer_distance = Inches(0.3)
        header = section.header.paragraphs[0]
        header.style = _MFI_STYLE_NAMES["header"]
        header.text = "MFI Drafter 2.0" + (f" - {country}" if country else "")
        footer = section.footer.paragraphs[0]
        footer.style = _MFI_STYLE_NAMES["footer"]
        footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer.add_run("Page ")
        _append_word_field(footer, "PAGE")
        footer.add_run(" of ")
        _append_word_field(footer, "NUMPAGES")


def _apply_mfi_paragraph_layout(paragraph: Any, layout: Dict[str, Any]) -> None:
    if not layout:
        return
    paragraph.paragraph_format.page_break_before = bool(
        layout.get("page_break_before")
    )
    paragraph.paragraph_format.keep_with_next = bool(layout.get("keep_with_next"))
    paragraph.paragraph_format.keep_together = bool(layout.get("keep_together"))
    if layout.get("compact_after"):
        paragraph.paragraph_format.space_after = Pt(2)


def _safe_filename(filename: str) -> str:
    name = filename.strip() or "export.docx"
    name = name.replace("\\", "_").replace("/", "_")
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name)
    if not name.lower().endswith(".docx"):
        name = name + ".docx"
    return name


def _add_text_lines(
    doc: Document,
    text: str,
    *,
    style: Optional[str] = None,
    layout: Optional[Dict[str, Any]] = None,
) -> None:
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("- ") or line.startswith("* "):
            paragraph = doc.add_paragraph(line[2:].strip(), style="List Bullet")
            _apply_mfi_paragraph_layout(paragraph, layout or {})
            continue

        if re.match(r"^\d+\.\s+", line):
            paragraph = doc.add_paragraph(
                re.sub(r"^\d+\.\s+", "", line), style="List Number"
            )
            _apply_mfi_paragraph_layout(paragraph, layout or {})
            continue

        paragraph = doc.add_paragraph(line, style=style)
        _apply_mfi_paragraph_layout(paragraph, layout or {})


def _get_continuous_score_rgb(score: float) -> tuple[int, int, int]:
    """Map a 0-10 score continuously from near-white to WFP blue."""
    ratio = max(0.0, min(float(score), 10.0)) / 10.0
    start = (239, 246, 255)
    end = (0, 114, 188)
    return tuple(
        round(start[index] + (end[index] - start[index]) * ratio)
        for index in range(3)
    )


def _set_cell_background(cell: Any, rgb_tuple: tuple[int, int, int]) -> None:
    r, g, b = rgb_tuple
    shading_elm = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{r:02x}{g:02x}{b:02x}"/>')
    cell._tc.get_or_add_tcPr().append(shading_elm)


def _add_overview_table_to_document(doc: Document, *, meta: Dict[str, Any]) -> None:
    dims = meta.get("dimensions") or []
    rows = meta.get("rows") or []
    if not isinstance(dims, list) or not isinstance(rows, list) or not dims or not rows:
        return

    sorted_rows = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        sorted_rows.append(r)
    sorted_rows = sorted(sorted_rows, key=lambda x: float(x.get("overall_mfi", 0) or 0))

    n_rows = len(sorted_rows) + 1
    n_cols = len(dims) + 3

    table = doc.add_table(rows=n_rows, cols=n_cols)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    headers = ["Market", "Region"] + [str(d) for d in dims] + ["MFI"]
    header_row = table.rows[0]
    for idx, header in enumerate(headers):
        cell = header_row.cells[idx]
        cell.text = str(header)
        _set_cell_background(cell, (0, 114, 188))
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in paragraph.runs:
                run.bold = True
                run.font.size = Pt(8)
                run.font.color.rgb = RGBColor(255, 255, 255)

    for row_idx, market in enumerate(sorted_rows, start=1):
        row = table.rows[row_idx]

        market_name = str(market.get("market_name", "") or "").strip()
        row.cells[0].text = market_name

        region = str(market.get("region", "") or "").strip()
        row.cells[1].text = region

        dim_scores = market.get("dimension_scores")
        if not isinstance(dim_scores, dict):
            dim_scores = {}

        for dim_idx, dim in enumerate(dims):
            cell = row.cells[dim_idx + 2]
            try:
                raw_score = dim_scores.get(dim)
                score = float(raw_score) if raw_score is not None else None
            except (TypeError, ValueError):
                score = None
            cell.text = f"{score:.2f}" if score is not None else "—"
            if score is not None:
                _set_cell_background(cell, _get_continuous_score_rgb(score))
            text_color = (
                RGBColor(255, 255, 255)
                if score is not None and score >= 6.5
                else RGBColor(0, 0, 0)
            )
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                for run in paragraph.runs:
                    run.font.size = Pt(8)
                    run.font.color.rgb = text_color

        mfi_cell = row.cells[-1]
        try:
            raw_mfi = market.get("overall_mfi")
            mfi_score = float(raw_mfi) if raw_mfi is not None else None
        except (TypeError, ValueError):
            mfi_score = None
        mfi_cell.text = f"{mfi_score:.2f}" if mfi_score is not None else "—"
        if mfi_score is not None:
            _set_cell_background(mfi_cell, _get_continuous_score_rgb(mfi_score))
        mfi_text_color = (
            RGBColor(255, 255, 255)
            if mfi_score is not None and mfi_score >= 6.5
            else RGBColor(0, 0, 0)
        )
        for paragraph in mfi_cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in paragraph.runs:
                run.font.size = Pt(8)
                run.font.color.rgb = mfi_text_color
                run.bold = True

        for cell_idx in [0, 1]:
            cell = row.cells[cell_idx]
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(8)

    doc.add_paragraph()


def _add_definition_box(
    doc: Document,
    text: str,
    *,
    mfi: bool = False,
    layout: Optional[Dict[str, Any]] = None,
) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        return

    table = doc.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    _set_table_row_cant_split(table.rows[0])

    cell = table.rows[0].cells[0]
    header_para = cell.paragraphs[0]
    if mfi:
        header_para.style = _MFI_STYLE_NAMES["definition"]
        _apply_mfi_paragraph_layout(header_para, layout or {})

    header_run = header_para.add_run("Definition: ")
    header_run.bold = True
    header_run.font.size = Pt(9)
    header_run.font.color.rgb = RGBColor(0, 114, 188)

    def_run = header_para.add_run(cleaned)
    def_run.italic = True
    def_run.font.size = Pt(9)

    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="E6F3FF"/>')
    cell._tc.get_or_add_tcPr().append(shading)

    if not mfi:
        doc.add_paragraph()


def _add_basket_definitions_table_to_document(doc: Document, *, meta: Dict[str, Any]) -> None:
    headers, rows = basket_definition_table_display(meta)
    if not headers or not rows:
        return
    table = doc.add_table(rows=len(rows) + 1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for column, header in enumerate(headers):
        cell = table.rows[0].cells[column]
        cell.text = header
        _set_cell_background(cell, (0, 114, 188))
        for paragraph in cell.paragraphs:
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            for run in paragraph.runs:
                run.bold = True
                run.font.size = Pt(8)
                run.font.color.rgb = RGBColor(255, 255, 255)
    for row_index, values in enumerate(rows, start=1):
        for column, value in enumerate(values):
            cell = table.rows[row_index].cells[column]
            cell.text = value
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(8)
    doc.add_paragraph()


def _add_mfi_presentation_table(
    doc: Document,
    *,
    meta: Dict[str, Any],
    layout: Optional[Dict[str, Any]] = None,
) -> None:
    """Render an already projected MFI table without inferring or formatting cells."""
    rows = meta.get("rows") or []
    if not isinstance(rows, list) or not rows:
        return
    spec_id = str(meta.get("spec_id") or "").strip()
    columns = [str(column) for column in meta.get("columns", []) or []]
    column_specs = meta.get("column_specs") or []
    if not spec_id or not columns or not isinstance(column_specs, list):
        raise ValueError("Projected MFI tables require a spec ID and explicit columns")
    if len(columns) > 8 or len(column_specs) != len(columns):
        raise ValueError("Projected MFI table columns violate the renderer contract")
    spec_keys = [
        str(item.get("key") or "") if isinstance(item, dict) else ""
        for item in column_specs
    ]
    if spec_keys != columns:
        raise ValueError("Projected MFI table columns do not match column_specs")
    values = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
            raise ValueError("Projected MFI table contains a malformed row")
        if any(column not in row["values"] for column in columns):
            raise ValueError("Projected MFI table row is missing a visible column")
        values.append(row["values"])
    title = str(meta.get("title") or "").strip()
    if title:
        title_paragraph = doc.add_paragraph(
            title,
            style=_MFI_STYLE_NAMES["minor_heading"],
        )
        title_paragraph.paragraph_format.keep_with_next = True
    table = doc.add_table(rows=len(values) + 1, cols=len(columns))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    total_width = sum(float(item["width_hint"]) for item in column_specs)
    widths = [6.5 * float(item["width_hint"]) / total_width for item in column_specs]
    alignment = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
    }
    for index, column in enumerate(columns):
        cell = table.rows[0].cells[index]
        cell.text = str(column_specs[index].get("label") or "")
        _set_table_cell_width(cell, widths[index])
        _set_cell_background(cell, (0, 114, 188))
        for paragraph in cell.paragraphs:
            paragraph.style = _MFI_STYLE_NAMES["table_header"]
            paragraph.alignment = alignment.get(
                str(column_specs[index].get("alignment") or "left"),
                WD_ALIGN_PARAGRAPH.LEFT,
            )
            for run in paragraph.runs:
                run.bold = True
                run.font.size = Pt(7)
                run.font.color.rgb = RGBColor(255, 255, 255)
    _set_repeat_table_header(table.rows[0])
    for row_index, row in enumerate(values, start=1):
        _set_table_row_cant_split(table.rows[row_index])
        for column_index, column in enumerate(columns):
            cell = table.rows[row_index].cells[column_index]
            _set_table_cell_width(cell, widths[column_index])
            cell.text = str(row[column])
            for paragraph in cell.paragraphs:
                paragraph.style = _MFI_STYLE_NAMES["table_body"]
                paragraph.alignment = alignment.get(
                    str(column_specs[column_index].get("alignment") or "left"),
                    WD_ALIGN_PARAGRAPH.LEFT,
                )
                for run in paragraph.runs:
                    run.font.size = Pt(7)


def _set_repeat_table_header(row: Any) -> None:
    properties = row._tr.get_or_add_trPr()
    element = OxmlElement("w:tblHeader")
    element.set(qn("w:val"), "true")
    properties.append(element)


def _set_table_row_cant_split(row: Any) -> None:
    properties = row._tr.get_or_add_trPr()
    properties.append(OxmlElement("w:cantSplit"))


def _set_table_cell_width(cell: Any, width_inches: float) -> None:
    cell.width = Inches(width_inches)
    properties = cell._tc.get_or_add_tcPr()
    width = properties.get_or_add_tcW()
    width.set(qn("w:w"), str(int(round(width_inches * 1440))))
    width.set(qn("w:type"), "dxa")


def _add_notice_box(
    doc: Document,
    text: str,
    *,
    label: str,
    fill: str,
    color: tuple[int, int, int],
    mfi: bool = False,
    layout: Optional[Dict[str, Any]] = None,
) -> None:
    cleaned = (text or "").strip()
    if not cleaned:
        return
    table = doc.add_table(rows=1, cols=1)
    table.style = "Table Grid"
    _set_table_row_cant_split(table.rows[0])
    cell = table.rows[0].cells[0]
    paragraph = cell.paragraphs[0]
    if mfi:
        paragraph.style = _MFI_STYLE_NAMES["notice"]
        _apply_mfi_paragraph_layout(paragraph, layout or {})
    label_run = paragraph.add_run(f"{label}: ")
    label_run.bold = True
    label_run.font.color.rgb = RGBColor(*color)
    text_run = paragraph.add_run(cleaned)
    text_run.font.size = Pt(9)
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{fill}"/>')
    cell._tc.get_or_add_tcPr().append(shading)
    if not mfi:
        doc.add_paragraph()


def build_docx_bytes_from_report_blocks(
    report_blocks: List[ReportBlock],
    *,
    visualizations: Optional[Dict[str, str]] = None,
    include_sources: bool = True,
    include_visualizations: bool = True,
    language: str = "en",
) -> bytes:
    doc = Document()
    visualizations = visualizations or {}
    mfi_document = _is_mfi_document(report_blocks)
    if mfi_document:
        title_layout = next(
            (
                _mfi_layout(block)
                for block in report_blocks
                if _mfi_layout(block).get("role") == "title"
            ),
            {},
        )
        _configure_mfi_document(doc, title_layout)

    for block in report_blocks:
        layout = _mfi_layout(block)
        if block.type == "heading":
            level = int(block.level or 1)
            level = min(max(level, 1), 9)
            if mfi_document and layout:
                role = str(layout.get("role") or "subsection")
                style_role = (
                    role
                    if role in {
                        "title",
                        "major_section",
                        "subsection",
                        "minor_heading",
                    }
                    else "subsection"
                )
                paragraph = doc.add_paragraph(
                    block.text or "",
                    style=_MFI_STYLE_NAMES[style_role],
                )
                _apply_mfi_paragraph_layout(paragraph, layout)
            else:
                doc.add_heading(block.text or "", level=level)
            continue

        if block.type == "paragraph":
            role = str(layout.get("role") or "body")
            style = (
                _MFI_STYLE_NAMES["claim" if role == "claim" else "body"]
                if mfi_document and layout
                else None
            )
            _add_text_lines(
                doc,
                block.text or "",
                style=style,
                layout=layout,
            )
            continue

        if block.type == "figure":
            if not include_visualizations:
                continue

            fig_id = (block.figure_id or "").strip()
            fig_b64 = visualizations.get(fig_id)
            if not fig_id or not fig_b64:
                continue

            try:
                img_bytes = base64.b64decode(fig_b64)
            except Exception:
                continue

            width = float(block.width) if block.width is not None else 6.0
            width = max(1.0, min(width, 7.0))

            buf = io.BytesIO(img_bytes)
            p = doc.add_paragraph()
            if mfi_document:
                p.style = _MFI_STYLE_NAMES["body"]
            run = p.add_run()
            run.add_picture(buf, width=Inches(width))
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _apply_mfi_paragraph_layout(p, layout)

            if block.caption:
                cap = doc.add_paragraph(
                    block.caption,
                    style=(
                        _MFI_STYLE_NAMES["caption"] if mfi_document else None
                    ),
                )
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                if mfi_document:
                    cap.paragraph_format.keep_together = True
            continue

        if block.type == "references":
            if not include_sources:
                continue

            refs = block.references or []
            if not refs:
                continue

            if mfi_document:
                heading = doc.add_paragraph(
                    t(language, "section.REFERENCES"),
                    style=_MFI_STYLE_NAMES["subsection"],
                )
                heading.paragraph_format.keep_with_next = True
            else:
                doc.add_heading(t(language, "section.REFERENCES"), level=2)
            for ref in refs:
                if not isinstance(ref, dict):
                    continue

                doc_id = str(ref.get("doc_id", "")).strip()
                source = str(ref.get("source", "")).strip()
                date = str(ref.get("date", "")).strip()
                title = str(ref.get("title", "")).strip()
                url = str(ref.get("url", "")).strip()

                parts: List[str] = []
                if doc_id:
                    parts.append(f"[{doc_id}]")
                if source:
                    parts.append(source)
                if date:
                    parts.append(f"({date})")
                if title:
                    parts.append(title)

                paragraph = doc.add_paragraph(
                    " ".join(parts).strip(),
                    style="List Number",
                )
                if mfi_document:
                    paragraph.paragraph_format.space_after = Pt(2)
                if url:
                    paragraph = doc.add_paragraph(
                        url,
                        style=(
                            _MFI_STYLE_NAMES["body"] if mfi_document else None
                        ),
                    )
            continue

        if block.type == "table":
            meta = block.meta or {}
            if isinstance(meta, dict) and meta.get("table_kind") == "mfi_overview":
                _add_overview_table_to_document(doc, meta=meta)
            elif isinstance(meta, dict) and meta.get("table_kind") == "basket_definitions":
                _add_basket_definitions_table_to_document(doc, meta=meta)
            elif isinstance(meta, dict) and meta.get("table_kind") == "mfi_presentation":
                _add_mfi_presentation_table(doc, meta=meta, layout=layout)
            elif isinstance(meta, dict) and meta.get("table_kind") == "mfi_deterministic":
                raise ValueError(
                    "Unprojected canonical MFI tables cannot be rendered in DOCX"
                )
            continue

        if block.type == "definition_box":
            _add_definition_box(
                doc,
                block.text or "",
                mfi=mfi_document,
                layout=layout,
            )
            continue

        if block.type == "evidence_note":
            paragraph = doc.add_paragraph(
                style=(
                    _MFI_STYLE_NAMES["evidence_note"] if mfi_document else None
                )
            )
            run = paragraph.add_run(f"Evidence: {block.text or ''}")
            run.italic = True
            run.font.size = Pt(8)
            run.font.color.rgb = RGBColor(80, 80, 80)
            _apply_mfi_paragraph_layout(paragraph, layout)
            continue

        if block.type == "limitation_box":
            _add_notice_box(
                doc,
                block.text or "",
                label="Data limitation",
                fill="FFF4CC",
                color=(145, 94, 0),
                mfi=mfi_document,
                layout=layout,
            )
            continue

        if block.type == "methodology_note":
            _add_notice_box(
                doc,
                block.text or "",
                label="Methodology",
                fill="E6F3FF",
                color=(0, 114, 188),
                mfi=mfi_document,
                layout=layout,
            )
            continue

        if block.type == "qa_warning":
            _add_notice_box(
                doc,
                block.text or "",
                label="QA warning",
                fill="FDE8E8",
                color=(176, 0, 32),
                mfi=mfi_document,
                layout=layout,
            )
            continue

        if block.type == "claim_warning":
            meta = block.meta or {}
            is_withdrawn = (
                isinstance(meta, dict)
                and meta.get("disposition")
                == "replaced_by_deterministic_fallback"
            )
            _add_notice_box(
                doc,
                block.text or "",
                label="Claim warning",
                fill="FDE8E8" if is_withdrawn else "FFF4CC",
                color=(176, 0, 32) if is_withdrawn else (145, 94, 0),
                mfi=mfi_document,
                layout=layout,
            )
            continue

    out = io.BytesIO()
    doc.save(out)
    out.seek(0)
    return out.read()


def build_content_disposition(filename: str) -> str:
    safe = _safe_filename(filename)
    return f'attachment; filename="{safe}"'
