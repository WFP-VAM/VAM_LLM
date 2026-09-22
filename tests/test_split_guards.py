"""Guards for the September 2026 split: the validators belong to MarketAIssist.

These tests fail if MFI/Price validator code, routes or navigation ever come
back into this repository. The validators live in
https://github.com/WFP-VAM/MarketAIssist and are not maintained here.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from app.streamlit_backend.dispatcher import dispatch_request

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_SERVICES = {"market-monitor", "mfi-drafter", "seasonal-outlook"}
VALIDATOR_IDENTIFIERS = re.compile(
    r"mfi_validator|price_validator|mfi-validator|price-validator"
)
# Live code and live documentation. `specs/` and `evals/` are dated records that
# predate the split and still describe the validators: they are deliberately
# left untouched and therefore not scanned.
SCANNED_DIRS = ("app", "pages", "tests", "scripts", "docs", "deploy")
SCANNED_SUFFIXES = {".py", ".md", ".txt", ".yaml", ".yml", ".json", ".sh", ".conf", ".template", ".tf"}
SKIPPED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules"}


def test_fastapi_exposes_exactly_the_three_drafters() -> None:
    payload = TestClient(main.app).get("/").json()
    assert {service["id"] for service in payload["services"]} == EXPECTED_SERVICES


def test_fastapi_has_no_validator_routes() -> None:
    paths = [route.path for route in main.app.routes if hasattr(route, "path")]
    assert not [p for p in paths if "validator" in p], paths


def test_dispatcher_lists_exactly_the_three_drafters() -> None:
    response = dispatch_request("GET", "/")
    assert response.status_code == 200
    import json

    payload = json.loads(response.content)
    assert {service["id"] for service in payload["services"]} == EXPECTED_SERVICES


@pytest.mark.parametrize(
    "path",
    [
        "/mfi-validator/info",
        "/mfi-validator/health",
        "/mfi-validator/validate-file",
        "/price-validator/info",
        "/price-validator/health",
        "/price-validator/products",
    ],
)
def test_dispatcher_rejects_validator_paths(path: str) -> None:
    assert dispatch_request("GET", path).status_code == 404


@pytest.mark.parametrize("module", ["app.services.mfi_validator", "app.services.price_validator"])
def test_validator_packages_are_gone(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def _live_files():
    for name in SCANNED_DIRS:
        base = REPO_ROOT / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in SCANNED_SUFFIXES:
                if not set(path.relative_to(REPO_ROOT).parts) & SKIPPED_PARTS:
                    yield path
    for path in REPO_ROOT.glob("*"):
        if path.is_file() and (path.suffix.lower() in SCANNED_SUFFIXES or path.name == ".env.example"):
            yield path


def test_no_live_source_file_references_the_validators() -> None:
    offenders = []
    for path in _live_files():
        relative = path.relative_to(REPO_ROOT)
        if relative.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if VALIDATOR_IDENTIFIERS.search(line):
                offenders.append(f"{relative.as_posix()}:{number}: {line.strip()[:120]}")
    assert not offenders, "validator references are back:\n" + "\n".join(offenders)


def test_validator_pages_and_assets_are_gone() -> None:
    missing = [
        "pages/1_MFI_Validator.py",
        "pages/2_Price_Validator.py",
        "app/shared/assets/mfidata_validator.jpeg",
        "app/shared/assets/pricedata_validator.jpeg",
        "tests/test_price_validator_market_names.py",
    ]
    present = [name for name in missing if (REPO_ROOT / name).exists()]
    assert not present, present
