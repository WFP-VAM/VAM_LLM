"""Typed errors for the live MFI generation pipeline."""

from __future__ import annotations


class MFIRunError(RuntimeError):
    """A report that cannot start or finish; the caller generates it again."""

    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code
