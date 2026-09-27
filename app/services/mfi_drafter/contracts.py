"""Small section-level contracts for the lightweight MFI workflow."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from functools import lru_cache
from pydantic import BaseModel, ConfigDict, Field

WORKFLOW = "mfi-light-v1"
BUNDLE = "mfi-light-contracts-v1"
MODEL = "gemini-3.1-pro-preview"
MAX_CHARACTERS = 1_200_000
MAX_INPUT_TOKENS = 250_000
MAX_OUTPUT_TOKENS = 65_536
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


class ContractConfigurationError(ValueError):
    """A programming/configuration failure, never a model repair target."""


def expanded_schema(model):
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})
    def expand(value):
        if isinstance(value, list):
            return [expand(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return expand(definitions[value["$ref"].rsplit("/", 1)[-1]])
        return {key: expand(item) for key, item in value.items() if key != "$defs"}
    return expand(schema)


def compile_provider_schema(schema):
    """Translate the supported transport subset; retain stricter checks in Pydantic.

    Annotation/default/extra-key/string-length rules are intentionally enforced
    by the application. Unknown structural constructs fail instead of weakening
    the response schema through the SDK's warning-and-drop behavior.
    """
    allowed = {"type", "properties", "items", "required", "enum", "description",
               "nullable", "minimum", "maximum", "minItems", "maxItems", "format"}
    application_only = {"title", "default", "additionalProperties", "minLength", "maxLength"}
    def convert(value):
        if "anyOf" in value:
            choices = [item for item in value["anyOf"] if item.get("type") != "null"]
            if len(choices) != 1 or len(value["anyOf"]) != 2:
                raise ContractConfigurationError("Response schema requires a concrete type or a nullable concrete type")
            return {**convert(choices[0]), "nullable": True}
        unknown = set(value) - allowed - application_only - {"const"}
        if unknown:
            raise ContractConfigurationError(f"Unsupported response schema keywords: {sorted(unknown)}")
        result = {key: deepcopy(item) for key, item in value.items() if key in allowed}
        if "const" in value:
            if isinstance(value["const"], str):
                result["enum"] = [value["const"]]
            else:
                result["description"] = f"Must equal {value['const']!r}; checked by application validation."
        if "properties" in result:
            result["properties"] = {key: convert(item) for key, item in result["properties"].items()}
            if len(result["properties"]) > 1:
                # Without an explicit order Vertex generates properties alphabetically, as these contracts
                # always have (notes before sections); the SDK would otherwise send the declaration order.
                result["propertyOrdering"] = sorted(result["properties"])
        if "items" in result:
            result["items"] = convert(result["items"])
        if result.get("type") == "object" and not result.get("properties"):
            raise ContractConfigurationError("Model-facing objects must have explicit properties")
        return result
    result = convert(schema)
    from app.shared.llm import check_response_schema
    try:
        check_response_schema(result)
    except Exception as exc:
        raise ContractConfigurationError("Installed Vertex SDK cannot encode the response contract") from exc
    return result


@lru_cache(maxsize=32)
def _compiled_schema(model):
    return compile_provider_schema(expanded_schema(model))


def provider_schema(model):
    # SDKs may rewrite schema dictionaries in place while encoding them.
    # Never pass the registry's cached object to a provider or caller.
    return deepcopy(_compiled_schema(model))


def response_schema(review=False):
    return provider_schema(ReviewResponse if review else SectionsResponse)


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
