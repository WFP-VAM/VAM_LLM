"""The unit a report is made of. Drafters build lists of blocks; the Word renderer and the pages lay them out."""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel


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
