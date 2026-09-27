"""Streamlit parts of the Price Bulletin page that belong to the Market Monitor: its report tables."""
from typing import Any, Dict

import pandas as pd
import streamlit as st

from .report_blocks import basket_definition_table_display


def render_basket_definitions(meta: Dict[str, Any]) -> None:
    headers, rows = basket_definition_table_display(meta)
    if headers and rows:
        st.dataframe(
            pd.DataFrame(rows, columns=headers),
            width="stretch",
            hide_index=True,
        )


# How each Market Monitor table kind appears in the report preview.
REPORT_TABLES = {"basket_definitions": render_basket_definitions}
