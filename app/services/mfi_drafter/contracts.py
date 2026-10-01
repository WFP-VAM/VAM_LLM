"""Small section-level contracts for the lightweight MFI workflow."""
from __future__ import annotations

import json
import re
from pydantic import BaseModel, ConfigDict, Field

WORKFLOW = "mfi-light-v1"
BUNDLE = "mfi-light-contracts-v1"
NODES = (
    "prepare_analysis", "context_retrieval", "charts", "draft_dimensions",
    "draft_markets", "review_dimensions", "review_markets", "correct_dimensions",
    "correct_markets", "executive_summary", "assemble_report",
)


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    section_id: str
    text_markdown: str = Field(min_length=1)


class SectionsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sections: list[Section]
    notes: list[str] = Field(default_factory=list)


class ReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    needs_revision: bool
    review_markdown: str = Field(min_length=1)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def response_schema(review=False):
    """The complete application contract, before any provider translation."""
    return (ReviewResponse if review else SectionsResponse).model_json_schema()


def parse_response(raw):
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)[:-3].strip()
    return json.loads(text)


def inspect_sections(payload, expected, sources):
    """Retain valid sections; report missing/ambiguous sections without claim schemas."""
    rows = payload.get("sections") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}, ["Response must contain a sections array"]
    valid, issues = {}, []
    if set(payload) - {"sections", "notes"} or not isinstance(payload.get("notes", []), list) or any(not isinstance(n, str) for n in payload.get("notes", [])):
        issues.append("Only sections and a notes string array are permitted")
    if any(not isinstance(row, dict) for row in rows):
        issues.append("Every section must be an object")
    ids = [r.get("section_id") for r in rows if isinstance(r, dict)]
    for section_id in expected:
        matches = [r for r in rows if isinstance(r, dict) and r.get("section_id") == section_id]
        if len(matches) != 1:
            issues.append(f"{section_id}: expected exactly one section")
            continue
        try:
            section = Section.model_validate(matches[0])
        except ValueError:
            issues.append(f"{section_id}: section_id and nonempty text_markdown strings required")
            continue
        text = section.text_markdown.strip()
        unknown = set(re.findall(r"\[(S\d+)\]", text)) - set(sources)
        urls = re.findall(r"\]\((https?://[^\s)]+)\)", text)
        allowed_urls = {s.get("url") for s in sources.values()}
        if not text or unknown or any(url not in allowed_urls for url in urls):
            issues.append(f"{section_id}: empty text or unavailable source citation")
        else:
            valid[section_id] = text
    if any(not isinstance(i, str) or i not in expected for i in ids):
        issues.append("Unknown section identifiers in response")
    return valid, issues
