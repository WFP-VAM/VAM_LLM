from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

from app.services.market_monitor.i18n import format_decimal_value, format_month_label, t


class ReportBlock(BaseModel):
    type: Literal[
        "heading",
        "paragraph",
        "figure",
        "references",
        "table",
        "limitation_box",
        "methodology_note",
    ]
    text: Optional[str] = None
    level: Optional[int] = None
    figure_id: Optional[str] = None
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    width: Optional[float] = None
    references: Optional[List[Dict[str, Any]]] = None
    meta: Optional[Dict[str, Any]] = None


def resolve_mfi_report_blocks(result: Dict[str, Any]) -> List[ReportBlock]:
    """Return the report blocks stored with a completed MFI result."""
    stored = result.get("report_blocks")
    if not isinstance(stored, list) or not stored:
        raise ValueError("This MFI result has no report blocks; reports from the previous workflow are no longer supported")
    return [
        item if isinstance(item, ReportBlock) else ReportBlock.model_validate(item)
        for item in stored
    ]


class MFIReportLayoutHint(BaseModel):
    """Typed internal layout contract shared by MFI renderers."""

    model_config = ConfigDict(frozen=True)

    report_family: Literal["mfi"] = "mfi"
    role: Literal[
        "title",
        "major_section",
        "subsection",
        "minor_heading",
        "body",
        "figure",
        "table",
        "notice",
        "references",
    ]
    group_id: Optional[str] = None
    page_break_before: bool = False
    keep_with_next: bool = False
    keep_together: bool = False
    compact_after: bool = False
    country: Optional[str] = None
    methodology_version: Optional[str] = None


_MFI_MAJOR_PAGE_BREAK_HEADINGS = {"Executive summary", "MFI dimensions"}


_INSERT_FIGURE_RE = re.compile(r"\[INSERT GRAPH:\s*([A-Za-z0-9_\-]+)\s*\]", flags=re.IGNORECASE)


def _sanitize_text(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = text.replace("**", "")
    text = text.replace("__", "")
    return text.strip()


def _text_to_paragraph_blocks(text: str) -> List[ReportBlock]:
    cleaned = _sanitize_text(text)
    if not cleaned:
        return []

    blocks: List[ReportBlock] = []
    parts = re.split(r"\n\s*\n+", cleaned)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        blocks.append(ReportBlock(type="paragraph", text=part))
    return blocks


def _blocks_from_text_with_figures(
    text: str,
    *,
    visualizations: Optional[Dict[str, Any]] = None,
    figure_aliases: Optional[Dict[str, str]] = None,
) -> List[ReportBlock]:
    cleaned = _sanitize_text(text)
    if not cleaned:
        return []

    blocks: List[ReportBlock] = []
    last = 0
    for m in _INSERT_FIGURE_RE.finditer(cleaned):
        before = cleaned[last : m.start()]
        blocks.extend(_text_to_paragraph_blocks(before))

        fig_id = m.group(1).strip()
        if figure_aliases and fig_id in figure_aliases:
            fig_id = figure_aliases[fig_id]
        if fig_id and (visualizations is None or visualizations.get(fig_id)):
            blocks.append(ReportBlock(type="figure", figure_id=fig_id))

        last = m.end()

    blocks.extend(_text_to_paragraph_blocks(cleaned[last:]))
    return blocks


def _basket_snapshots_for_report(result: Dict[str, Any]) -> List[tuple[str, Dict[str, Any]]]:
    baskets = result.get("food_baskets") or {}
    primary = baskets.get("primary") if isinstance(baskets, dict) else None
    if not isinstance(primary, dict) or not primary:
        legacy = result.get("food_basket")
        primary = legacy if isinstance(legacy, dict) and legacy else None
    selected: List[tuple[str, Dict[str, Any]]] = []
    if isinstance(primary, dict) and primary:
        selected.append(("primary", dict(primary)))
    secondary = baskets.get("secondary") if isinstance(baskets, dict) else None
    if bool(result.get("secondary_basket_included")) and isinstance(secondary, dict) and secondary:
        selected.append(("secondary", dict(secondary)))
    return selected


def _basket_component_payload(item: Dict[str, Any], index: int) -> Dict[str, Any]:
    name = str(
        item.get("commodity_name_snapshot")
        or item.get("commodity_name")
        or item.get("name")
        or ""
    ).strip()
    quantity = item.get("weight_quantity")
    try:
        quantity_value = float(quantity)
    except (TypeError, ValueError):
        quantity_value = None
    return {
        "commodity_id": item.get("commodity_id"),
        "commodity_name": name,
        "unit_id": item.get("databridges_unit_id") or item.get("unit_id"),
        "unit": str(item.get("databridges_unit") or item.get("unit") or "").strip(),
        "quantity": quantity_value,
        "note": item.get("item_note") or item.get("note"),
        "sort_order": item.get("sort_order") or index,
    }


def _basket_component_label(component: Dict[str, Any], language: str) -> str:
    quantity = component.get("quantity")
    if quantity is None:
        quantity_label = ""
    elif language == "en":
        quantity_label = f"{float(quantity):g}"
    else:
        quantity_label = format_decimal_value(quantity, language, decimals=2).rstrip("0").rstrip(",.")
    parts = [quantity_label, str(component.get("unit") or "").strip(), str(component.get("commodity_name") or "").strip()]
    return " ".join(part for part in parts if part)


def _basket_definition_table_meta(result: Dict[str, Any], language: str) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for role, snapshot in _basket_snapshots_for_report(result):
        raw_items = [item for item in snapshot.get("items") or [] if isinstance(item, dict)]
        components = [_basket_component_payload(item, index) for index, item in enumerate(raw_items, start=1)]
        components.sort(key=lambda item: (int(item.get("sort_order") or 0), int(item.get("commodity_id") or 0)))
        regions = [str(region).strip() for region in snapshot.get("regions") or [] if str(region).strip()]
        scope_type = str(snapshot.get("scope_type") or "national").strip().lower() or "national"
        if scope_type == "national":
            scope_label = t(language, "basket.scope.national")
        elif regions:
            scope_label = t(language, "basket.scope.selected_regions_named", regions=", ".join(regions))
        else:
            scope_label = t(language, "basket.scope.selected_regions")
        name = str(snapshot.get("basket_name") or ("MEB" if role == "primary" else "")).strip()
        role_label = t(language, f"basket.role.{role}")
        rows.append(
            {
                "basket_role": role,
                "role_label": role_label,
                "basket_name": name,
                "short_description": str(snapshot.get("short_description") or "").strip(),
                "scope_type": scope_type,
                "scope_label": scope_label,
                "regions": regions,
                "components": components,
                "basket_version_id": snapshot.get("basket_version_id"),
                "version_number": snapshot.get("version_number"),
                "display": {
                    "basket": f"{name} ({role_label})",
                    "description": str(snapshot.get("short_description") or "").strip(),
                    "scope": scope_label,
                    "composition": "; ".join(
                        label for label in (_basket_component_label(item, language) for item in components) if label
                    ),
                },
            }
        )
    return {
        "table_kind": "basket_definitions",
        "language": language,
        "columns": ["basket", "description", "scope", "composition"],
        "headers": {
            "basket": t(language, "basket.table.header.basket"),
            "description": t(language, "basket.table.header.description"),
            "scope": t(language, "basket.table.header.scope"),
            "composition": t(language, "basket.table.header.composition"),
        },
        "rows": rows,
    }


def basket_definition_table_display(meta: Dict[str, Any]) -> tuple[List[str], List[List[str]]]:
    columns = [str(item) for item in meta.get("columns") or []]
    headers_by_key = meta.get("headers") or {}
    headers = [str(headers_by_key.get(key) or key) for key in columns]
    display_rows: List[List[str]] = []
    for row in meta.get("rows") or []:
        if not isinstance(row, dict):
            continue
        display = row.get("display") or {}
        display_rows.append([str(display.get(key) or "") for key in columns])
    return headers, display_rows


def _basket_row(meta: Dict[str, Any], role: str) -> Optional[Dict[str, Any]]:
    return next(
        (
            row
            for row in meta.get("rows") or []
            if isinstance(row, dict) and row.get("basket_role") == role
        ),
        None,
    )


def _basket_figure_caption(
    meta: Dict[str, Any],
    role: str,
    kind: str,
    *,
    language: str,
    time_period: str,
) -> str:
    row = _basket_row(meta, role)
    if row is None and role == "primary" and kind == "trend":
        return t(language, "figure.food_basket_trend")
    if row is None:
        return ""
    key = "figure.basket_trend_role" if kind == "trend" else "figure.basket_regional_role"
    kwargs = {
        "basket": row.get("basket_name") or ("MEB" if role == "primary" else ""),
        "scope": row.get("scope_label") or "",
        "period": format_month_label(time_period, language),
    }
    return t(language, key, **kwargs)


def _available_basket_figure(
    visualizations: Dict[str, Any],
    canonical_id: str,
    legacy_id: Optional[str] = None,
) -> Optional[str]:
    if visualizations.get(canonical_id):
        return canonical_id
    if legacy_id and visualizations.get(legacy_id):
        return legacy_id
    return None


def _module_section_title(module_id: Any, language: str = "en") -> str:
    module_key = str(module_id or "").strip()
    known_titles = {
        "exchange_rate": "module.exchange_rate",
        "fuel_energy": "module.fuel_energy",
        "livestock_animal_products": "module.livestock_animal_products",
        "labour_market": "module.labour_market",
    }
    if module_key in known_titles:
        return t(language, known_titles[module_key])
    words = [word for word in re.split(r"[_\-\s]+", module_key) if word]
    base = " ".join(word.capitalize() for word in words) if words else "Module"
    if base.lower().endswith(" analysis"):
        return base
    if language == "en":
        return f"{base} Analysis"
    return base


def build_market_monitor_report_blocks(result: Dict[str, Any]) -> List[ReportBlock]:
    country = (result.get("country") or "").strip()
    time_period = (result.get("time_period") or "").strip()
    language = str(result.get("language") or "en").strip().lower() or "en"
    display_period = time_period if language == "en" else format_month_label(time_period, language)

    title = t(language, "report.title")
    if country and display_period:
        title = f"{title} - {country} - {display_period}"
    elif country:
        title = f"{title} - {country}"

    sections = result.get("report_draft_sections") or result.get("report_sections") or {}
    module_sections = result.get("module_sections") or {}
    document_references = result.get("document_references") or []
    visualizations = result.get("visualizations") or {}
    visualizations = visualizations if isinstance(visualizations, dict) else {}
    basket_meta = _basket_definition_table_meta(result, language)

    blocks: List[ReportBlock] = [ReportBlock(type="heading", text=title, level=1)]

    highlights = sections.get("HIGHLIGHTS")
    if isinstance(highlights, str) and highlights.strip():
        blocks.append(ReportBlock(type="heading", text=t(language, "section.HIGHLIGHTS"), level=2))
        blocks.extend(_text_to_paragraph_blocks(highlights))

    if basket_meta.get("rows"):
        blocks.append(
            ReportBlock(
                type="heading",
                text=t(language, "section.BASKET_DEFINITIONS"),
                level=2,
            )
        )
        blocks.append(ReportBlock(type="table", meta=basket_meta))

    primary_trend = _available_basket_figure(
        visualizations,
        "food_basket_trend_primary",
        "food_basket_trend",
    )
    if primary_trend:
        blocks.append(
            ReportBlock(
                type="figure",
                figure_id=primary_trend,
                caption=_basket_figure_caption(
                    basket_meta,
                    "primary",
                    "trend",
                    language=language,
                    time_period=time_period,
                ),
            )
        )
    secondary_trend = _available_basket_figure(
        visualizations,
        "food_basket_trend_secondary",
    )
    if secondary_trend and bool(result.get("secondary_basket_included")):
        blocks.append(
            ReportBlock(
                type="figure",
                figure_id=secondary_trend,
                caption=_basket_figure_caption(
                    basket_meta,
                    "secondary",
                    "trend",
                    language=language,
                    time_period=time_period,
                ),
            )
        )

    overview = sections.get("MARKET_OVERVIEW")
    if isinstance(overview, str) and overview.strip():
        blocks.append(ReportBlock(type="heading", text=t(language, "section.MARKET_OVERVIEW"), level=2))
        blocks.extend(_text_to_paragraph_blocks(overview))

    commodity = sections.get("COMMODITY_ANALYSIS")
    if isinstance(commodity, str) and commodity.strip():
        blocks.append(ReportBlock(type="heading", text=t(language, "section.COMMODITY_ANALYSIS"), level=2))
        blocks.extend(_blocks_from_text_with_figures(commodity, visualizations=visualizations))

        has_inline_commodity_figs = False
        for m in _INSERT_FIGURE_RE.finditer(_sanitize_text(commodity)):
            fig_id = (m.group(1) or "").strip()
            if fig_id.lower().startswith("commodity_trends"):
                has_inline_commodity_figs = True
                break

        if not has_inline_commodity_figs and isinstance(visualizations, dict) and visualizations:
            ids = [
                k
                for k in visualizations.keys()
                if isinstance(k, str) and k.startswith("commodity_trends_")
            ]

            if ids:
                cat_order = [
                    "cereals",
                    "pulses",
                    "oil",
                    "sugar",
                    "condiments",
                    "vegetables",
                    "livestock",
                    "other",
                ]

                def sort_key(fig_id: str) -> tuple:
                    m = re.match(r"^commodity_trends_([a-z0-9_]+)_p(\d+)$", fig_id)
                    if not m:
                        return (99, fig_id, 0)
                    cat = m.group(1)
                    try:
                        page = int(m.group(2))
                    except Exception:
                        page = 0
                    try:
                        cat_rank = cat_order.index(cat)
                    except ValueError:
                        cat_rank = 50
                    return (cat_rank, cat, page)

                for fig_id in sorted(ids, key=sort_key):
                    blocks.append(ReportBlock(type="figure", figure_id=fig_id))

    regional = sections.get("REGIONAL_HIGHLIGHTS")
    primary_regional = _available_basket_figure(
        visualizations,
        "regional_comparison_primary",
        "regional_comparison",
    )
    secondary_regional = _available_basket_figure(
        visualizations,
        "regional_comparison_secondary",
    )
    has_regional_text = isinstance(regional, str) and bool(regional.strip())
    if has_regional_text or primary_regional or (secondary_regional and bool(result.get("secondary_basket_included"))):
        blocks.append(ReportBlock(type="heading", text=t(language, "section.REGIONAL_HIGHLIGHTS"), level=2))
        regional_blocks = (
            _blocks_from_text_with_figures(
                regional,
                visualizations=visualizations,
                figure_aliases={
                    "regional_comparison": primary_regional or "regional_comparison",
                },
            )
            if has_regional_text
            else []
        )
        blocks.extend(regional_blocks)
        inserted_ids = {
            block.figure_id
            for block in regional_blocks
            if block.type == "figure" and block.figure_id
        }
        if primary_regional and primary_regional not in inserted_ids:
            blocks.append(
                ReportBlock(
                    type="figure",
                    figure_id=primary_regional,
                    caption=_basket_figure_caption(
                        basket_meta,
                        "primary",
                        "regional",
                        language=language,
                        time_period=time_period,
                    ),
                )
            )
        if (
            secondary_regional
            and bool(result.get("secondary_basket_included"))
            and secondary_regional not in inserted_ids
        ):
            blocks.append(
                ReportBlock(
                    type="figure",
                    figure_id=secondary_regional,
                    caption=_basket_figure_caption(
                        basket_meta,
                        "secondary",
                        "regional",
                        language=language,
                        time_period=time_period,
                    ),
                )
            )

    if isinstance(module_sections, dict):
        for module_id, section_text in module_sections.items():
            if not isinstance(section_text, str) or not section_text.strip():
                continue
            blocks.append(ReportBlock(type="heading", text=_module_section_title(module_id, language), level=2))
            blocks.extend(_text_to_paragraph_blocks(section_text))
            if module_id == "fuel_energy" and isinstance(visualizations, dict) and visualizations.get("fuel_prices"):
                blocks.append(
                    ReportBlock(
                        type="figure",
                        figure_id="fuel_prices",
                        caption=t(language, "figure.fuel_prices"),
                    )
                )
            if (
                module_id == "livestock_animal_products"
                and isinstance(visualizations, dict)
                and visualizations.get("livestock_animal_products")
            ):
                blocks.append(
                    ReportBlock(
                        type="figure",
                        figure_id="livestock_animal_products",
                        caption=t(language, "figure.livestock_animal_products"),
                    )
                )
            if module_id == "labour_market" and isinstance(visualizations, dict) and visualizations.get("labour_market"):
                blocks.append(
                    ReportBlock(
                        type="figure",
                        figure_id="labour_market",
                        caption=t(language, "figure.labour_market"),
                    )
                )

    if document_references:
        blocks.append(ReportBlock(type="references", references=document_references))

    return blocks


def _apply_mfi_layout_contract(
    blocks: List[ReportBlock],
    *,
    country: str,
    methodology_version: str,
) -> List[ReportBlock]:
    """Attach explicit R8 layout roles without making renderers infer semantics."""
    for block in blocks:
        meta = dict(block.meta or {})
        role: str
        page_break_before = False
        keep_with_next = False
        keep_together = False
        compact_after = False
        if block.type == "heading":
            if int(block.level or 1) <= 1:
                role = "title"
            elif int(block.level or 2) == 2:
                role = "major_section"
                page_break_before = str(block.text or "") in (
                    _MFI_MAJOR_PAGE_BREAK_HEADINGS
                )
            elif int(block.level or 3) == 3:
                role = "subsection"
            else:
                role = "minor_heading"
            keep_with_next = True
        elif block.type == "figure":
            role = "figure"
            keep_with_next = bool(block.caption)
            keep_together = True
            compact_after = True
        elif block.type == "table":
            role = "table"
            compact_after = True
        elif block.type == "references":
            role = "references"
        elif block.type in {"limitation_box", "methodology_note"}:
            role = "notice"
            keep_together = True
            compact_after = True
        else:
            role = "body"
        layout = MFIReportLayoutHint(
            role=role,
            page_break_before=page_break_before,
            keep_with_next=keep_with_next,
            keep_together=keep_together,
            compact_after=compact_after,
            country=country or None if role == "title" else None,
            methodology_version=(
                methodology_version if role == "title" else None
            ),
        )
        meta["mfi_layout"] = layout.model_dump(mode="json")
        block.meta = meta
    return blocks
