"""Guards for the shared layer: code in app/shared serves every drafter and depends on none of them."""
from __future__ import annotations

import ast
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1] / "app" / "shared"


def _imported_modules(path: Path) -> list[str]:
    modules = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            modules.append(prefix + (node.module or ""))
    return modules


def test_shared_code_imports_no_drafter() -> None:
    offenders = [
        f"{path.relative_to(SHARED.parents[1]).as_posix()}: {module}"
        for path in SHARED.rglob("*.py")
        for module in _imported_modules(path)
        if module.startswith("app.services") or module.lstrip(".").startswith("services")
    ]
    assert offenders == []
