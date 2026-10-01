"""Why a model call produced no usable reply, as stable codes, and which provider errors are worth retrying."""
from __future__ import annotations

from typing import Dict, Optional

TRANSPORT = "llm_transport_error"
EMPTY = "llm_empty_or_unreadable_response"
TRUNCATED = "llm_response_truncated"
INVALID_JSON = "llm_invalid_json"
CONTRACT = "llm_response_contract_error"
REQUEST_PERSISTENCE = "llm_request_persistence_error"
RESPONSE_PERSISTENCE = "llm_response_persistence_error"

# A reply came back but was unusable: the failures that run diagnostics count as contract failures.
CONTRACT_FAILURES = frozenset({EMPTY, TRUNCATED, INVALID_JSON, CONTRACT, "llm_response_blocked", "llm_response_unknown"})

class LLMCallError(RuntimeError):
    """A failed call. `failure_code` and `stage` are stable; the message never carries prompt or reply text."""

    def __init__(
        self,
        *,
        failure_code: str,
        call_id: str,
        node: str,
        operation: str,
        stage: str,
        transient: bool = False,
        raw_text: Optional[str] = None,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
    ) -> None:
        self.failure_code = failure_code
        self.call_id = call_id
        self.node = node
        self.operation = operation
        self.stage = stage
        self.transient = transient
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        # Kept in memory for the caller only: never in public metadata, logs or the message.
        self.raw_text = raw_text
        super().__init__(f"LLM call failed [{failure_code}] at {node}/{operation} (call_id={call_id})")

    def to_public_dict(self) -> Dict[str, str]:
        return {
            "code": "llm_call_failed",
            "failure_code": self.failure_code,
            "call_id": self.call_id,
            "node": self.node,
            "operation": self.operation,
            "stage": self.stage,
            **({"artifact_type": self.artifact_type} if self.artifact_type else {}),
            **({"artifact_id": self.artifact_id} if self.artifact_id else {}),
        }


def is_transient(error: BaseException) -> bool:
    """Timeouts, rate limits, server-side and network failures. Never permission, argument or not-found errors."""
    if isinstance(error, ProviderError):
        return error.transient
    try:
        import httpx

        if isinstance(error, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
    except ImportError:
        pass
    return isinstance(error, (TimeoutError, ConnectionError))


class ProviderError(RuntimeError):
    """Adapter failure independent of the SDK; never a content-repair target."""
    def __init__(self, message: str, *, kind: str = "transport", transient: bool = False):
        self.kind, self.transient = kind, transient
        super().__init__(message)
