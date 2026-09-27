"""Reading a reply: the JSON object inside it, or a content-free description of JSON that failed to parse."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# Finish reasons meaning the model stopped at its output limit.
TRUNCATED_FINISH_REASONS = {"MAX_TOKENS", "LENGTH"}


def parse_json_object(raw_text: str) -> Dict[str, Any]:
    """Extract the first complete-looking JSON object, tolerating code fences."""
    cleaned = re.sub(r"```json\s*", "", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```", "", cleaned).strip()
    start_index = cleaned.find("{")
    end_index = cleaned.rfind("}")
    if start_index == -1 or end_index == -1 or end_index < start_index:
        raise ValueError("LLM response does not contain a JSON object")
    parsed = json.loads(cleaned[start_index : end_index + 1])
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON root must be an object")
    return parsed


def inspect_json_structure(raw_text: str, error: Optional[Exception] = None) -> Dict[str, Optional[int]]:
    """Describe JSON shape without retaining or emitting response content."""
    cleaned = re.sub(r"```json\s*", "", raw_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```", "", cleaned).strip()
    start_index = cleaned.find("{")
    candidate = cleaned[start_index:] if start_index >= 0 else cleaned
    decoder = json.JSONDecoder()
    cursor = 0
    roots = 0
    first_end: Optional[int] = None
    while cursor < len(candidate):
        while cursor < len(candidate) and candidate[cursor].isspace():
            cursor += 1
        if cursor >= len(candidate):
            break
        try:
            _value, end = decoder.raw_decode(candidate, cursor)
        except json.JSONDecodeError:
            break
        roots += 1
        if first_end is None:
            first_end = end
        cursor = end

    decode_error = error if isinstance(error, json.JSONDecodeError) else None
    return {
        "json_root_value_count": roots,
        "json_code_fence_count": raw_text.count("```"),
        "json_trailing_character_count": (
            len(candidate[first_end:].strip()) if first_end is not None else None
        ),
        "json_error_line": getattr(decode_error, "lineno", None),
        "json_error_column": getattr(decode_error, "colno", None),
        "json_error_position": getattr(decode_error, "pos", None),
    }
