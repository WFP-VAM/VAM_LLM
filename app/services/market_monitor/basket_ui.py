"""Pure helpers for the Price Bulletin basket configuration UI.

The Streamlit page intentionally keeps rendering and HTTP requests local.  This
module owns the deterministic view-model, validation, commodity selection, and
session-state rules so they can be exercised without importing a Streamlit
script.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from typing import Any


PRIMARY_ROLE = "primary"
SECONDARY_ROLE = "secondary"
NATIONAL_SCOPE = "national"
SELECTED_REGIONS_SCOPE = "selected_regions"
DEFAULT_PRIMARY_NAME = "MEB"

_LAST_COUNTRY_KEY = "mm_basket_last_country"
_ITERATION_PREFIX = "mm_report_iteration_"
_SECONDARY_VERSION_PREFIX = "mm_basket_secondary_version_"


class BasketUIValidationError(ValueError):
    """One or more basket-editor fields are invalid."""

    def __init__(self, errors: Sequence[str]):
        self.errors = [str(error) for error in errors if str(error).strip()]
        super().__init__(" ".join(self.errors))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _case_key(value: Any) -> str:
    return _text(value).casefold()


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def basket_item_name(item: Any) -> str:
    if not isinstance(item, Mapping):
        return ""
    return _text(item.get("commodity_name_snapshot") or item.get("commodity_name"))


def basket_items(basket: Any) -> list[Mapping[str, Any]]:
    if not isinstance(basket, Mapping):
        return []
    items = basket.get("items")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, Mapping)]


def commodity_catalog(raw_commodities: Any) -> dict[int, dict[str, Any]]:
    """Return priced commodity metadata keyed by stable commodity ID."""
    catalog: dict[int, dict[str, Any]] = {}
    if not isinstance(raw_commodities, list):
        return catalog
    for raw in raw_commodities:
        if not isinstance(raw, Mapping) or raw.get("priced") is False:
            continue
        commodity_id = _int_or_none(raw.get("id"))
        name = _text(raw.get("name"))
        if commodity_id is None or commodity_id in catalog or not name:
            continue
        catalog[commodity_id] = {
            "id": commodity_id,
            "name": name,
            "unit": _text(raw.get("unit") or raw.get("unit_name")),
        }
    return catalog


def build_editor_rows(raw_commodities: Any, basket: Any) -> list[dict[str, Any]]:
    """Build deterministic rows and retain unavailable saved snapshots."""
    catalog = commodity_catalog(raw_commodities)
    saved_by_id: dict[int, Mapping[str, Any]] = {}
    for item in basket_items(basket):
        commodity_id = _int_or_none(item.get("commodity_id"))
        if commodity_id is not None and commodity_id not in saved_by_id:
            saved_by_id[commodity_id] = item

    rows: list[dict[str, Any]] = []
    for commodity_id, commodity in catalog.items():
        saved = saved_by_id.get(commodity_id) or {}
        rows.append(
            {
                "Include": bool(saved),
                "Available": True,
                "Commodity ID": commodity_id,
                "Commodity": commodity["name"],
                "Unit": commodity["unit"],
                "Quantity": float(saved.get("weight_quantity") or 0.0),
                "Note": _text(saved.get("item_note")),
            }
        )

    for commodity_id, saved in saved_by_id.items():
        if commodity_id in catalog:
            continue
        rows.append(
            {
                "Include": True,
                "Available": False,
                "Commodity ID": commodity_id,
                "Commodity": basket_item_name(saved) or f"Commodity {commodity_id}",
                "Unit": _text(saved.get("databridges_unit") or saved.get("unit")),
                "Quantity": float(saved.get("weight_quantity") or 0.0),
                "Note": _text(saved.get("item_note")),
            }
        )
    return rows


def unavailable_basket_regions(basket: Any, available_regions: Any) -> list[str]:
    if not isinstance(basket, Mapping):
        return []
    available = {_case_key(region) for region in available_regions or [] if _text(region)}
    return [
        _text(region)
        for region in basket.get("regions") or []
        if _text(region) and _case_key(region) not in available
    ]


def canonical_regions(selected: Any, available_regions: Any) -> tuple[list[str], list[str]]:
    available_by_key = {
        _case_key(region): _text(region)
        for region in available_regions or []
        if _text(region)
    }
    normalized: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for value in selected or []:
        text = _text(value)
        key = _case_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        canonical = available_by_key.get(key)
        if canonical is None:
            invalid.append(text)
        else:
            normalized.append(canonical)
    return normalized, invalid


def build_basket_save_payload(
    *,
    role: str,
    basket_name: Any,
    short_description: Any,
    scope_type: str,
    selected_regions: Any,
    available_regions: Any,
    edited_rows: Any,
    change_note: Any = None,
    created_by_user_id: str = "streamlit",
) -> dict[str, Any]:
    """Normalize an editor submission or raise actionable UI errors."""
    role_value = _case_key(role)
    errors: list[str] = []
    if role_value not in {PRIMARY_ROLE, SECONDARY_ROLE}:
        errors.append(f"Unsupported basket role: {role!r}.")

    name = _text(basket_name)
    if role_value == PRIMARY_ROLE:
        if not name or name.casefold() == DEFAULT_PRIMARY_NAME.casefold():
            name = DEFAULT_PRIMARY_NAME
    elif not name:
        errors.append("Second basket name is required.")

    description = _text(short_description) or None
    if role_value == PRIMARY_ROLE and name != DEFAULT_PRIMARY_NAME and not description:
        errors.append("A short description is required for a custom primary basket name.")
    if role_value == SECONDARY_ROLE and not description:
        errors.append("Second basket description is required.")

    scope_value = _case_key(scope_type)
    if scope_value not in {NATIONAL_SCOPE, SELECTED_REGIONS_SCOPE}:
        errors.append(f"Unsupported basket scope: {scope_type!r}.")
        scope_value = NATIONAL_SCOPE

    regions: list[str] = []
    if scope_value == SELECTED_REGIONS_SCOPE:
        regions, invalid_regions = canonical_regions(selected_regions, available_regions)
        if invalid_regions:
            errors.append("Unavailable basket regions: " + ", ".join(invalid_regions) + ".")
        if not regions:
            errors.append("Select at least one available region for a selected-region basket.")

    rows = edited_rows
    if hasattr(rows, "to_dict"):
        try:
            rows = rows.to_dict("records")
        except TypeError:
            rows = rows.to_dict()

    items: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for row in rows or []:
        if not isinstance(row, Mapping) or not row.get("Include"):
            continue
        commodity_id = _int_or_none(row.get("Commodity ID"))
        name_for_error = _text(row.get("Commodity")) or str(row.get("Commodity ID") or "Unknown")
        if commodity_id is None:
            errors.append(f"{name_for_error} has an invalid commodity ID.")
            continue
        if commodity_id in seen_ids:
            errors.append(f"Commodity {name_for_error} is selected more than once.")
            continue
        seen_ids.add(commodity_id)
        if row.get("Available") is False:
            errors.append(
                f"{name_for_error} is no longer priced in the active cache; remove or replace it before publishing."
            )
            continue
        try:
            quantity = float(row.get("Quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            errors.append(f"{name_for_error} needs a positive quantity.")
            continue
        items.append(
            {
                "commodity_id": commodity_id,
                "weight_quantity": quantity,
                "item_note": _text(row.get("Note")) or None,
            }
        )

    if not items and not any("positive quantity" in error for error in errors):
        errors.append("Select at least one available commodity for the basket.")

    if errors:
        raise BasketUIValidationError(errors)

    return {
        "basket_role": role_value,
        "basket_name": name,
        "short_description": description,
        "scope_type": scope_value,
        "regions": regions,
        "items": items,
        "change_note": _text(change_note) or None,
        "created_by_user_id": _text(created_by_user_id) or "streamlit",
    }


def basket_commodity_ids(basket: Any) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for item in basket_items(basket):
        commodity_id = _int_or_none(item.get("commodity_id"))
        if commodity_id is not None and commodity_id not in seen:
            seen.add(commodity_id)
            ids.append(commodity_id)
    return ids


def locked_commodity_ids(*baskets: Any) -> list[int]:
    locked: list[int] = []
    seen: set[int] = set()
    for basket in baskets:
        for commodity_id in basket_commodity_ids(basket):
            if commodity_id not in seen:
                seen.add(commodity_id)
                locked.append(commodity_id)
    return locked


def additional_commodity_ids(raw_commodities: Any, locked_ids: Iterable[int]) -> list[int]:
    catalog = commodity_catalog(raw_commodities)
    locked = {int(value) for value in locked_ids}
    return [commodity_id for commodity_id in catalog if commodity_id not in locked]


def sanitize_selected_commodity_ids(selected: Any, allowed_ids: Iterable[int]) -> list[int]:
    allowed = {int(value) for value in allowed_ids}
    normalized: list[int] = []
    seen: set[int] = set()
    for value in selected or []:
        commodity_id = _int_or_none(value)
        if commodity_id is not None and commodity_id in allowed and commodity_id not in seen:
            seen.add(commodity_id)
            normalized.append(commodity_id)
    return normalized


def run_commodity_names(
    raw_commodities: Any,
    *,
    included_baskets: Sequence[Any],
    additional_ids: Any,
) -> list[str]:
    """Convert the ID-based UI selection to the legacy name-based graph input."""
    catalog = commodity_catalog(raw_commodities)
    names: list[str] = []
    seen_names: set[str] = set()

    def append_name(value: Any) -> None:
        text = _text(value)
        key = text.casefold()
        if text and key not in seen_names:
            seen_names.add(key)
            names.append(text)

    for basket in included_baskets:
        for item in basket_items(basket):
            commodity_id = _int_or_none(item.get("commodity_id"))
            current = catalog.get(commodity_id) if commodity_id is not None else None
            append_name(current.get("name") if current else basket_item_name(item))
    for commodity_id in sanitize_selected_commodity_ids(additional_ids, catalog.keys()):
        append_name(catalog[commodity_id]["name"])
    return names


def scope_overlap_errors(
    included_baskets: Sequence[Any],
    *,
    report_regions: Any,
    available_regions: Any,
) -> list[str]:
    """Validate configured selected-region scopes against a proposed run."""
    available, _ = canonical_regions(available_regions, available_regions)
    selected, _ = canonical_regions(report_regions, available_regions)
    effective = selected if selected else available
    effective_keys = {_case_key(region) for region in effective}
    errors: list[str] = []

    for basket in included_baskets:
        if not isinstance(basket, Mapping):
            continue
        if _case_key(basket.get("scope_type")) != SELECTED_REGIONS_SCOPE:
            continue
        configured_keys = {
            _case_key(region)
            for region in basket.get("regions") or []
            if _text(region)
        }
        if configured_keys & effective_keys:
            continue
        role = _case_key(basket.get("basket_role")) or "basket"
        name = _text(basket.get("basket_name")) or role.title()
        if role == SECONDARY_ROLE:
            errors.append(
                f"The secondary basket {name!r} has no configured region in the selected report regions. "
                "Select an overlapping region or uncheck the secondary basket."
            )
        else:
            errors.append(
                f"The primary basket {name!r} has no configured region in the selected report regions. "
                "Select at least one overlapping region."
            )
    return errors


def role_state_prefix(country: str, role: str) -> str:
    return f"mm_basket_{_case_key(role)}_{_text(country)}_"


def clear_state_prefixes(state: MutableMapping[str, Any], *prefixes: str) -> None:
    for key in list(state.keys()):
        if any(str(key).startswith(prefix) for prefix in prefixes):
            state.pop(key, None)


def clear_role_state(state: MutableMapping[str, Any], country: str, role: str) -> None:
    clear_state_prefixes(state, role_state_prefix(country, role))


def report_iteration(state: MutableMapping[str, Any], country: str) -> int:
    key = f"{_ITERATION_PREFIX}{_text(country)}"
    try:
        value = int(state.get(key, 0))
    except (TypeError, ValueError):
        value = 0
    state[key] = value
    return value


def advance_report_iteration(state: MutableMapping[str, Any], country: str) -> int:
    key = f"{_ITERATION_PREFIX}{_text(country)}"
    value = report_iteration(state, country) + 1
    state[key] = value
    return value


def sync_report_iteration_context(
    state: MutableMapping[str, Any],
    *,
    country: str,
    secondary_version_id: Any,
) -> int:
    """Advance once when the selected country or secondary version changes."""
    country_value = _text(country)
    secondary_value = _text(secondary_version_id)
    should_advance = state.get(_LAST_COUNTRY_KEY) != country_value
    state[_LAST_COUNTRY_KEY] = country_value

    version_key = f"{_SECONDARY_VERSION_PREFIX}{country_value}"
    if version_key in state and _text(state.get(version_key)) != secondary_value:
        should_advance = True
    state[version_key] = secondary_value

    if should_advance:
        return advance_report_iteration(state, country_value)
    return report_iteration(state, country_value)


def inclusion_widget_key(country: str, secondary_version_id: Any, iteration: int) -> str:
    secondary = _text(secondary_version_id) or "none"
    return f"mm_include_secondary_{_text(country)}_{secondary}_{int(iteration)}"


def clear_secondary_inclusion_state(state: MutableMapping[str, Any], country: str) -> None:
    clear_state_prefixes(state, f"mm_include_secondary_{_text(country)}_")
    state.pop(f"{_SECONDARY_VERSION_PREFIX}{_text(country)}", None)
