"""Streamlit parts of the MFI Drafter page that belong to the MFI: its report tables and layout, and the
downloads of its complete analytical tables."""
from typing import Any, Dict

import pandas as pd
import streamlit as st

from .table_projection import build_mfi_raw_table_downloads


def report_layout(block: Dict[str, Any]) -> Dict[str, Any]:
    """The layout contract a stored report block carries (see report_layout.py)."""
    meta = block.get("meta")
    return (
        meta.get("mfi_layout")
        if isinstance(meta, dict)
        and isinstance(meta.get("mfi_layout"), dict)
        else {}
    )


def render_presentation_table(meta: Dict[str, Any]) -> None:
    rows = meta.get("rows") or []
    columns = [str(column) for column in meta.get("columns", []) or []]
    column_specs = meta.get("column_specs") or []
    if not str(meta.get("spec_id") or "").strip() or not columns:
        raise ValueError(
            "Projected MFI tables require a spec ID and explicit columns"
        )
    if not isinstance(column_specs, list) or [
        str(item.get("key") or "") if isinstance(item, dict) else ""
        for item in column_specs
    ] != columns:
        raise ValueError(
            "Projected MFI table columns do not match column_specs"
        )
    display_rows = []
    labels = [str(item.get("label") or "") for item in column_specs]
    for row in rows:
        values = row.get("values") if isinstance(row, dict) else None
        if not isinstance(values, dict) or any(
            column not in values for column in columns
        ):
            raise ValueError("Projected MFI table contains a malformed row")
        display_rows.append([values[column] for column in columns])
    if display_rows:
        title = str(meta.get("title") or "").strip()
        if title:
            st.markdown(f"**{title}**")
        dataframe = pd.DataFrame(display_rows, columns=labels)
        st.dataframe(dataframe, width="stretch", hide_index=True)


# How each MFI table kind appears in the report preview.
REPORT_TABLES = {"mfi_presentation": render_presentation_table}


def render_raw_table_downloads(
    assessment_profile: Any,
    *,
    key_prefix: str,
) -> None:
    """Render the complete canonical table bundle in Technical details."""
    if not isinstance(assessment_profile, dict):
        return
    st.markdown("**Complete analytical table downloads**")
    st.caption(
        "These files contain the complete unprojected Phase 2 tables at canonical "
        "precision; reader-facing report tables are selected and formatted projections."
    )
    for index, download in enumerate(
        build_mfi_raw_table_downloads(assessment_profile)
    ):
        st.download_button(
            str(download["label"]),
            data=download["data"],
            file_name=str(download["file_name"]),
            mime=str(download["mime"]),
            key=f"{key_prefix}_raw_table_{index}",
        )
