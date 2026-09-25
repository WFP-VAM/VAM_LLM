"""What goes to the model and what comes back, independent of the SDK that carries it."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Union


@dataclass(frozen=True)
class FilePart:
    """A stored file the model reads directly, such as a map image in Cloud Storage."""

    uri: str
    mime_type: str


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
    finish_reason: str = "STOP"
    usage: Dict[str, Optional[int]] = field(default_factory=dict)
    response_id: Optional[str] = None
    model_version: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


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
