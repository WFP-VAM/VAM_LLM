"""Typed fail-closed errors for the live MFI generation pipeline."""

from __future__ import annotations

from typing import Any, Dict, Optional


class MFIRunError(RuntimeError):
    """A report that cannot start or finish; the caller generates it again."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


class MFIGenerationBlockedError(RuntimeError):
    """Stop a live run before any report payload can be published."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str,
        status_code: int,
        artifact_type: Optional[str] = None,
        artifact_id: Optional[str] = None,
        field_name: Optional[str] = None,
        task_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        batch_kind: Optional[str] = None,
        shard_key: Optional[str] = None,
        character_count: Optional[int] = None,
        target_characters: Optional[int] = None,
        target_count: Optional[int] = None,
        call_id: Optional[str] = None,
        attempt: Optional[int] = None,
        batch_diagnostics: Optional[list[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.status_code = status_code
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.field_name = field_name
        self.task_id = task_id
        self.batch_id = batch_id
        self.batch_kind = batch_kind
        self.shard_key = shard_key
        self.character_count = character_count
        self.target_characters = target_characters
        self.target_count = target_count
        self.call_id = call_id
        self.attempt = attempt
        self.batch_diagnostics = list(batch_diagnostics or [])

    @property
    def node(self) -> str:
        return self.stage

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            key: value
            for key, value in {
                "code": self.code,
                "message": self.message,
                "stage": self.stage,
                "artifact_type": self.artifact_type,
                "artifact_id": self.artifact_id,
                "field_name": self.field_name,
                "task_id": self.task_id,
                "batch_id": self.batch_id,
                "batch_kind": self.batch_kind,
                "shard_key": self.shard_key,
                "character_count": self.character_count,
                "target_characters": self.target_characters,
                "target_count": self.target_count,
                "call_id": self.call_id,
                "attempt": self.attempt,
            }.items()
            if value is not None
        }


def claim_identity_blocked(
    message: str,
    *,
    stage: str,
    artifact_type: Optional[str] = None,
    artifact_id: Optional[str] = None,
) -> MFIGenerationBlockedError:
    return MFIGenerationBlockedError(
        "mfi_claim_identity_contract_failed",
        message,
        stage=stage,
        status_code=500,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
    )
