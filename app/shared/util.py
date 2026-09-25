"""Small helpers shared by the drafters."""
import re

_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|api[_-]?secret|client[_-]?secret|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def redact_secrets(text: str) -> str:
    """Replace the value of credential-like ``name: value`` or ``name=value`` pairs with ``[redacted]``."""
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
