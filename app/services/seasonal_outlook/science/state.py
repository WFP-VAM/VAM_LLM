"""Shared graph state. Clients, API keys and image bytes are never stored here."""
from typing import Literal, TypedDict


class WorkflowState(TypedDict, total=False):
    input_path: str
    arm: Literal['control', 'rules']
    run_dir: str
    pack: dict
    rules: list[dict]
    evidence: dict
    analysis: dict
    status: str
    errors: list
    failed_stage: str | None
    trace: list[dict]
    output: dict
