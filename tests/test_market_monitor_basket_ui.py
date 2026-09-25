from __future__ import annotations

import pytest

from app.services.market_monitor.basket_ui import (
    BasketUIValidationError,
    additional_commodity_ids,
    advance_report_iteration,
    build_basket_save_payload,
    build_editor_rows,
    clear_role_state,
    clear_secondary_inclusion_state,
    inclusion_widget_key,
    locked_commodity_ids,
    report_iteration,
    run_commodity_names,
    sanitize_selected_commodity_ids,
    scope_overlap_errors,
    sync_report_iteration_context,
    unavailable_basket_regions,
)


RAW_COMMODITIES = [
    {"id": 1, "name": "Maize", "unit": "kg", "priced": True},
    {"id": 2, "name": "Beans", "unit": "kg", "priced": True},
    {"id": 3, "name": "Oil", "unit": "litre", "priced": False},
]


def _basket(
    role="primary",
    *,
    basket_id="primary-v1",
    name="MEB",
    scope="national",
    regions=None,
    items=None,
):
    return {
        "basket_version_id": basket_id,
        "basket_role": role,
        "basket_name": name,
        "scope_type": scope,
        "regions": regions or [],
        "items": items
        or [
            {
                "commodity_id": 1,
                "commodity_name_snapshot": "Maize",
                "databridges_unit": "kg",
                "weight_quantity": 5,
            }
        ],
    }


def test_editor_rows_retain_unavailable_saved_snapshots():
    basket = _basket(
        items=[
            {"commodity_id": 1, "commodity_name_snapshot": "Old Maize", "weight_quantity": 2},
            {
                "commodity_id": 99,
                "commodity_name_snapshot": "Legacy cereal",
                "databridges_unit": "bag",
                "weight_quantity": 3,
            },
        ]
    )

    rows = build_editor_rows(RAW_COMMODITIES, basket)

    assert [row["Commodity ID"] for row in rows] == [1, 2, 99]
    assert rows[0] == {
        "Include": True,
        "Available": True,
        "Commodity ID": 1,
        "Commodity": "Maize",
        "Unit": "kg",
        "Quantity": 2.0,
        "Note": "",
    }
    assert rows[-1]["Available"] is False
    assert rows[-1]["Commodity"] == "Legacy cereal"


def test_primary_payload_normalizes_meb_and_discards_national_regions():
    payload = build_basket_save_payload(
        role="primary",
        basket_name=" meb ",
        short_description="",
        scope_type="national",
        selected_regions=["Juba"],
        available_regions=["Juba"],
        edited_rows=build_editor_rows(RAW_COMMODITIES, _basket()),
        change_note=" Updated quantities ",
    )

    assert payload["basket_role"] == "primary"
    assert payload["basket_name"] == "MEB"
    assert payload["short_description"] is None
    assert payload["regions"] == []
    assert payload["items"] == [
        {"commodity_id": 1, "weight_quantity": 5.0, "item_note": None}
    ]
    assert payload["change_note"] == "Updated quantities"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"role": "primary", "basket_name": "Urban basket"}, "custom primary"),
        ({"role": "secondary", "basket_name": ""}, "name is required"),
        (
            {"role": "secondary", "basket_name": "Emergency ration", "short_description": ""},
            "description is required",
        ),
        (
            {
                "scope_type": "selected_regions",
                "selected_regions": ["Missing"],
            },
            "Unavailable basket regions",
        ),
    ],
)
def test_editor_payload_validation(overrides, message):
    kwargs = {
        "role": "primary",
        "basket_name": "MEB",
        "short_description": "",
        "scope_type": "national",
        "selected_regions": [],
        "available_regions": ["Juba"],
        "edited_rows": build_editor_rows(RAW_COMMODITIES, _basket()),
    }
    kwargs.update(overrides)

    with pytest.raises(BasketUIValidationError, match=message):
        build_basket_save_payload(**kwargs)


def test_selected_region_payload_is_canonical_and_unavailable_items_block_publish():
    rows = build_editor_rows(
        RAW_COMMODITIES,
        _basket(
            items=[
                {
                    "commodity_id": 99,
                    "commodity_name_snapshot": "Legacy cereal",
                    "weight_quantity": 1,
                }
            ]
        ),
    )
    with pytest.raises(BasketUIValidationError, match="no longer priced"):
        build_basket_save_payload(
            role="primary",
            basket_name="MEB",
            short_description="",
            scope_type="selected_regions",
            selected_regions=[" juba ", "JUBA"],
            available_regions=["Juba", "Wau"],
            edited_rows=rows,
        )

    rows[-1]["Include"] = False
    rows[0]["Include"] = True
    rows[0]["Quantity"] = 2
    payload = build_basket_save_payload(
        role="primary",
        basket_name="MEB",
        short_description="",
        scope_type="selected_regions",
        selected_regions=[" juba ", "JUBA"],
        available_regions=["Juba", "Wau"],
        edited_rows=rows,
    )
    assert payload["regions"] == ["Juba"]


def test_unavailable_region_snapshots_are_reported():
    basket = _basket(scope="selected_regions", regions=["Juba", "Old State"])
    assert unavailable_basket_regions(basket, ["Juba", "Wau"]) == ["Old State"]


def test_id_based_locking_and_name_payload_keep_shared_components_separate():
    primary = _basket(items=[{"commodity_id": 1, "commodity_name_snapshot": "Old Maize"}])
    secondary = _basket(
        "secondary",
        basket_id="secondary-v1",
        name="Emergency ration",
        items=[
            {"commodity_id": 1, "commodity_name_snapshot": "Maize"},
            {"commodity_id": 2, "commodity_name_snapshot": "Beans"},
        ],
    )

    locked = locked_commodity_ids(primary, secondary)
    assert locked == [1, 2]
    assert additional_commodity_ids(RAW_COMMODITIES, locked) == []
    assert additional_commodity_ids(RAW_COMMODITIES, locked_commodity_ids(primary)) == [2]
    assert sanitize_selected_commodity_ids([2, 2, 99], [2]) == [2]
    assert run_commodity_names(
        RAW_COMMODITIES,
        included_baskets=[primary, secondary],
        additional_ids=[2],
    ) == ["Maize", "Beans"]


def test_scope_overlap_uses_all_regions_for_an_empty_run_selection():
    primary = _basket(scope="selected_regions", regions=["Juba"])
    secondary = _basket(
        "secondary",
        basket_id="secondary-v1",
        name="Urban basket",
        scope="selected_regions",
        regions=["Wau"],
    )

    assert scope_overlap_errors(
        [primary, secondary],
        report_regions=[],
        available_regions=["Juba", "Wau"],
    ) == []

    errors = scope_overlap_errors(
        [primary, secondary],
        report_regions=["Juba"],
        available_regions=["Juba", "Wau"],
    )
    assert len(errors) == 1
    assert "secondary basket 'Urban basket'" in errors[0]
    assert "uncheck" in errors[0]


def test_iteration_state_resets_only_for_context_changes_and_accepted_runs():
    state = {}
    first = sync_report_iteration_context(
        state,
        country="South Sudan",
        secondary_version_id="secondary-v1",
    )
    assert first == 1
    assert sync_report_iteration_context(
        state,
        country="South Sudan",
        secondary_version_id="secondary-v1",
    ) == first

    changed_version = sync_report_iteration_context(
        state,
        country="South Sudan",
        secondary_version_id="secondary-v2",
    )
    assert changed_version == first + 1
    assert inclusion_widget_key("South Sudan", "secondary-v2", changed_version).endswith(
        "secondary-v2_2"
    )

    accepted_run = advance_report_iteration(state, "South Sudan")
    assert accepted_run == changed_version + 1
    assert report_iteration(state, "South Sudan") == accepted_run

    sync_report_iteration_context(state, country="Ethiopia", secondary_version_id="")
    switched_back = sync_report_iteration_context(
        state,
        country="South Sudan",
        secondary_version_id="secondary-v2",
    )
    assert switched_back == accepted_run + 1


def test_role_and_inclusion_state_cleanup_is_scoped():
    state = {
        "mm_basket_secondary_Sudan_editing": True,
        "mm_basket_primary_Sudan_editing": True,
        "mm_basket_secondary_Ethiopia_editing": True,
        "mm_include_secondary_Sudan_v1_1": False,
        "mm_basket_secondary_version_Sudan": "v1",
    }

    clear_role_state(state, "Sudan", "secondary")
    assert "mm_basket_secondary_Sudan_editing" not in state
    assert "mm_basket_primary_Sudan_editing" in state
    assert "mm_basket_secondary_Ethiopia_editing" in state

    clear_secondary_inclusion_state(state, "Sudan")
    assert "mm_include_secondary_Sudan_v1_1" not in state
    assert "mm_basket_secondary_version_Sudan" not in state

