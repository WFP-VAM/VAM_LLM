"""Minimal local contracts. No provider-specific types or automatic interpretation."""
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Signal(Record):
    evidence_id: str
    geography: str
    signal: str
    magnitude_as_read: str | None
    legend_class: str | None
    location_in_figure: str
    confidence: Literal["high", "medium", "low"]
    limitations: list[str]


class MapReading(Record):
    figure_id: str
    title_as_read: str
    product_kind: Literal["observed", "forecast", "mixed", "other", "unknown"]
    variable: str
    metric: str
    units: str | None
    reference_baseline: str | None
    period_as_read: str
    valid_start: str | None
    valid_end: str | None
    issue_date: str | None
    geographic_coverage: str
    legend_as_read: str
    readability: Literal["readable", "partial", "unreadable"]
    signals: list[Signal]
    limitations: list[str]


class EvidenceBundle(Record):
    case_id: str
    maps: list[MapReading]


class StudyRule(Record):
    rule_id: str
    layer: Literal["extraction", "analysis", "writing"]
    title: str
    instruction_en: str
    applicability: str
    required_inputs: list[str]
    examples: list[str]
    counterexamples: list[str]
    limitations: list[str]
    origin: Literal["user_decision", "semantic_constraint", "corpus_pattern", "candidate"]
