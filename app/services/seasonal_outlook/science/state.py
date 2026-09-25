"""Graph state for one Seasonal phase. Clients, API keys and image bytes are never stored here."""
from typing import Literal, TypedDict


class SeasonalState(TypedDict, total=False):
    phase: Literal['extract', 'feedback', 'report']
    pack: dict
    images: list[dict]  # GCS references with figure notes, never the bytes
    arm: Literal['rules']
    rules: list[dict]
    evidence_profile: dict
    report_profile: dict
    evidence_v1: dict
    review: dict
    evidence: dict
    issue_resolutions: list[dict]
    allocations: list[dict]
    analyst_comments: str
    feedback_resolutions: list[dict]
    initial_analysis: dict
    draft_review: dict
    report: dict
    artifacts: dict
