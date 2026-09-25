"""Prompt registry for localized Market Monitor drafting prompts."""
from __future__ import annotations

import hashlib
import json
import string
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Set

from .i18n import SUPPORTED_REPORT_LANGUAGES, normalize_language

PROMPT_ROOT = Path(__file__).with_name("prompts")
MANIFEST_PATH = PROMPT_ROOT / "manifest.json"

OUTPUT_FACING_PROMPTS = {
    "exchange_rate",
    "fuel_energy",
    "livestock_animal_products",
    "labour_market",
    "highlights",
    "narrative",
    "red_team",
}


class PromptRegistryError(RuntimeError):
    pass


def _prompt_path(prompt_id: str, language: str) -> Path:
    safe_id = str(prompt_id or "").strip()
    if not safe_id:
        raise PromptRegistryError("Prompt id is required")
    lang = normalize_language(language)
    return PROMPT_ROOT / lang / f"{safe_id}.txt"


@lru_cache(maxsize=64)
def get_prompt_template(prompt_id: str, language: str) -> str:
    path = _prompt_path(prompt_id, language)
    if not path.exists():
        raise PromptRegistryError(f"Missing prompt template: {path}")
    return path.read_text(encoding="utf-8")


def template_placeholders(template: str) -> Set[str]:
    placeholders: Set[str] = set()
    formatter = string.Formatter()
    for _literal, field_name, _format_spec, _conversion in formatter.parse(template):
        if field_name:
            placeholders.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return placeholders


def render_prompt(prompt_id: str, language: str, context: Dict[str, Any]) -> str:
    template = get_prompt_template(prompt_id, language)
    missing = sorted(template_placeholders(template) - set(context.keys()))
    if missing:
        raise PromptRegistryError(f"Missing prompt placeholders for {prompt_id}.{language}: {missing}")
    try:
        return template.format(**context)
    except KeyError as exc:
        raise PromptRegistryError(f"Missing prompt placeholder for {prompt_id}.{language}: {exc}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_prompt_manifest() -> Dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise PromptRegistryError(f"Missing prompt manifest: {MANIFEST_PATH}")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def validate_prompt_manifest(*, strict_hashes: bool = True) -> List[str]:
    manifest = load_prompt_manifest()
    errors: List[str] = []
    prompts = manifest.get("prompts") or {}

    for prompt_id in sorted(OUTPUT_FACING_PROMPTS):
        entry = prompts.get(prompt_id)
        if not isinstance(entry, dict):
            errors.append(f"Missing manifest entry for prompt: {prompt_id}")
            continue
        languages = entry.get("languages") or []
        if set(languages) != SUPPORTED_REPORT_LANGUAGES:
            errors.append(f"{prompt_id}: expected languages {sorted(SUPPORTED_REPORT_LANGUAGES)}, got {languages}")
            continue

        expected_placeholders = set(entry.get("placeholders") or [])
        hashes = entry.get("hashes") or {}
        translations_of = entry.get("translations_of") or {}
        current_en_hash = None

        for language in sorted(SUPPORTED_REPORT_LANGUAGES):
            path = _prompt_path(prompt_id, language)
            if not path.exists():
                errors.append(f"{prompt_id}.{language}: missing template file")
                continue

            template = path.read_text(encoding="utf-8")
            placeholders = template_placeholders(template)
            if placeholders != expected_placeholders:
                errors.append(
                    f"{prompt_id}.{language}: placeholder mismatch "
                    f"expected={sorted(expected_placeholders)} actual={sorted(placeholders)}"
                )

            if strict_hashes:
                current_hash = _sha256(path)
                if hashes.get(language) != current_hash:
                    errors.append(f"{prompt_id}.{language}: hash mismatch")
                if language == "en":
                    current_en_hash = current_hash

        if strict_hashes and current_en_hash:
            for language in ("fr", "es"):
                if translations_of.get(language) != current_en_hash:
                    errors.append(f"{prompt_id}.{language}: translation is not synced to current English hash")

    return errors


def assert_prompt_manifest_valid() -> None:
    errors = validate_prompt_manifest(strict_hashes=True)
    if errors:
        raise PromptRegistryError("; ".join(errors))
