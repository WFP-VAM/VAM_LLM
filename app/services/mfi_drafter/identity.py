"""Stable, application-owned tokens for the names used in analytical fact identifiers."""
from __future__ import annotations

from hashlib import sha256
import re
import unicodedata


def normalized_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value))
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_")
    return slug or "unnamed"


def context_token(value: str) -> str:
    """Return the Phase 2 collision-resistant normalized context token."""
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    digest = sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{normalized_slug(normalized)}_{digest}"
