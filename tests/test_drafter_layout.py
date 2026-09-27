"""The three drafters keep one layout: graph.py builds the graph, nodes/ holds a module per node type, prompts.py
holds the prompt texts.

graph.py imports the node modules. A node module that imported graph.py back would make an import cycle and put
wiring inside a node, so none may; the graph is built nowhere else.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRAFTERS = ("market_monitor", "mfi_drafter", "seasonal_outlook")


def package(drafter):
    return ROOT / "app" / "services" / drafter


def imported_modules(path, module):
    """The absolute modules a file imports, with `from package import name` counted as package.name."""
    is_package = path.name == "__init__.py"
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = module.split(".")
                base = parts[: len(parts) - node.level + (1 if is_package else 0)]
                source = ".".join([*base, node.module] if node.module else base)
            else:
                source = node.module or ""
            found.add(source)
            found.update(f"{source}.{alias.name}" for alias in node.names)
    return found


@pytest.mark.parametrize("drafter", DRAFTERS)
def test_each_drafter_has_a_graph_node_modules_and_prompts(drafter):
    assert (package(drafter) / "graph.py").is_file()
    assert (package(drafter) / "prompts.py").is_file()
    assert (package(drafter) / "nodes" / "__init__.py").is_file()
    assert [p for p in (package(drafter) / "nodes").glob("*.py") if p.name != "__init__.py"]


@pytest.mark.parametrize("drafter", DRAFTERS)
def test_no_node_module_imports_the_graph(drafter):
    graph = f"app.services.{drafter}.graph"
    offenders = []
    for path in sorted((package(drafter) / "nodes").glob("*.py")):
        stem = "" if path.name == "__init__.py" else f".{path.stem}"
        module = f"app.services.{drafter}.nodes{stem}"
        if any(name == graph or name.startswith(graph + ".") for name in imported_modules(path, module)):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


@pytest.mark.parametrize("drafter", DRAFTERS)
def test_only_graph_py_builds_the_graph(drafter):
    builders = sorted(path.relative_to(package(drafter)).as_posix() for path in package(drafter).rglob("*.py")
                      if "__pycache__" not in path.parts and "StateGraph(" in path.read_text(encoding="utf-8"))
    assert builders == ["graph.py"]
