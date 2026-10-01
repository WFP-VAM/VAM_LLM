"""What goes to the model and what comes back, independent of the SDK that carries it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional, Sequence, Union

from app.shared.runs.store import ObjectRef


@dataclass(frozen=True)
class FilePart:
    """An immutable original object; only a shared adapter resolves its location."""

    reference: ObjectRef

    @property
    def mime_type(self) -> str:
        return self.reference["mime"]


@dataclass
class LLMRequest:
    """One model call as a drafter describes it. The drafter's profile supplies the model settings."""

    operation: str
    node: str
    parts: Sequence[Union[str, FilePart]]
    system: Optional[str] = None
    response_schema: Optional[Dict[str, Any]] = None
    json_output: bool = False
    max_output_tokens: Optional[int] = None
    timeout_seconds: Optional[float] = None
    fail_on_truncation: bool = False
    artifact_type: Optional[str] = None
    artifact_id: Optional[str] = None
    work_item: Optional[str] = None
    correction_attempt: int = 0
    # A drafter that runs its own attempts (MFI) links each one to the failed call it retries or repairs.
    retry_of: Optional[str] = None
    repair_of: Optional[str] = None


@dataclass
class LLMResponse:
    """The model's reply: its text without thought parts, why it stopped, and token usage."""

    text: str
    outcome: Literal["completed", "truncated", "blocked", "unknown"]
    finish_reason: Optional[str] = None
    usage: Dict[str, Optional[int]] = field(default_factory=lambda: dict(prompt_tokens=None, candidate_tokens=None,
                                                                       thought_tokens=None, total_tokens=None))
    response_id: Optional[str] = None
    model_version: Optional[str] = None
    raw: Any = field(default_factory=dict)

    def __post_init__(self):
        if self.outcome not in {"completed", "truncated", "blocked", "unknown"}:
            raise ValueError("Invalid normalized model outcome")
        self.usage = {**dict(prompt_tokens=None, candidate_tokens=None, thought_tokens=None, total_tokens=None), **self.usage}


@dataclass
class LLMResult:
    """A call that succeeded: its id, the reply, and what parsing and validation made of it."""

    call_id: str
    response: LLMResponse
    payload: Any = None
    value: Any = None

    @property
    def raw_text(self) -> str:
        return self.response.text.strip()


@dataclass(frozen=True)
class RequestMeasurement:
    characters: int
    input_tokens: Optional[int]
    fingerprint: str
    max_characters: Optional[int]
    max_input_tokens: Optional[int]

    @property
    def allowed(self) -> bool:
        return ((self.max_characters is None or self.characters <= self.max_characters)
                and (self.max_input_tokens is None or
                     self.input_tokens is not None and self.input_tokens <= self.max_input_tokens))
