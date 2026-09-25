from __future__ import annotations

import builtins
from copy import deepcopy
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest


PAGE = Path(__file__).resolve().parents[1] / "pages" / "3_Price_Bulletin_Drafter.py"


def _basket(role, version_id, name, item_id, item_name, *, scope="national", regions=None):
    return {
        "basket_version_id": version_id,
        "country_iso3": "SSD",
        "version_number": 1 if role == "primary" else 2,
        "basket_role": role,
        "basket_name": name,
        "short_description": "Primary MEB reference basket." if role == "primary" else "Urban affordability proxy.",
        "scope_type": scope,
        "regions": regions or [],
        "status": "active",
        "created_at": "2026-07-01T10:00:00Z",
        "created_by_user_id": "tester",
        "cache_version_id_at_creation": "cache-v1",
        "change_note": None,
        "items": [
            {
                "basket_item_id": f"item-{version_id}",
                "basket_version_id": version_id,
                "commodity_id": item_id,
                "commodity_name_snapshot": item_name,
                "databridges_unit": "kg",
                "weight_quantity": 5.0,
                "sort_order": 0,
                "item_note": None,
            }
        ],
    }


class FakePriceBulletinBackend:
    def __init__(self, *, primary=True, secondary=True, second_basket_enabled=True):
        self.primary = (
            _basket("primary", "primary-v1", "MEB", 1, "Maize") if primary else None
        )
        self.secondary = (
            _basket(
                "secondary",
                "secondary-v1",
                "Urban basket",
                2,
                "Beans",
                scope="selected_regions",
                regions=["Juba"],
            )
            if secondary
            else None
        )
        self.calls = []
        self.reportability_params = []
        self.run_payloads = []
        self.reject_run = False
        self.joint_latest_override = None
        self.second_basket_enabled = second_basket_enabled
        self.refresh_status = "updated"

    def configuration(self):
        return {
            "country": "South Sudan",
            "iso3": "SSD",
            "primary": deepcopy(self.primary),
            "secondary": deepcopy(self.secondary),
            "needs_primary_setup": self.primary is None,
            "has_secondary": self.secondary is not None,
            "second_basket_enabled": self.second_basket_enabled,
        }

    def request_json(self, method, path, **kwargs):
        payload = deepcopy(kwargs.get("json_body"))
        self.calls.append((method, path, payload))
        if path == "/market-monitor/cache/status":
            return {
                "status": "active",
                "has_active_cache": True,
                "active_version_id": "cache-v1",
                "active_country_count": 1,
                "rows_prices": 36,
            }
        if path == "/market-monitor/countries":
            return {
                "countries": [
                    {
                        "name": "South Sudan",
                        "iso3": "SSD",
                        "has_data": True,
                        "currency_code": "SSP",
                    }
                ]
            }
        if path.endswith("/metadata"):
            return {
                "country": "South Sudan",
                "iso3": "SSD",
                "commodities": [
                    {"id": 1, "name": "Maize", "unit": "kg", "priced": True},
                    {"id": 2, "name": "Beans", "unit": "kg", "priced": True},
                    {"id": 3, "name": "Salt", "unit": "kg", "priced": True},
                ],
                "default_commodities": ["Salt"],
                "regions": ["Juba", "Wau"],
                "latest_cached_date": "2025-02-01",
                "cache_version_id": "cache-v1",
                "warnings": [],
            }
        if path.endswith("/baskets") and method == "GET":
            return self.configuration()
        if path.endswith("/baskets/primary") and method == "POST":
            self.primary = self._saved_basket("primary", "primary-v2", payload)
            return self.configuration()
        if path.endswith("/baskets/secondary") and method == "POST":
            self.secondary = self._saved_basket("secondary", "secondary-v2", payload)
            return self.configuration()
        if path.endswith("/baskets/secondary") and method == "DELETE":
            archived = deepcopy(self.secondary)
            self.secondary = None
            return {**self.configuration(), "archived_secondary": archived}
        if path.endswith("/reportable-months") and method == "GET":
            self.reportability_params.append(deepcopy(kwargs.get("params") or {}))
            joint_latest = self.joint_latest_override or "2025-02"
            joint_months = ["2025-01", "2025-02"] if joint_latest == "2025-02" else ["2025-01"]
            return {
                "reportable_months": joint_months,
                "latest_reportable_month": joint_latest,
                "primary_reportable_months": ["2025-01", "2025-02"],
                "secondary_reportable_months": ["2025-01", "2025-02"],
                "joint_reportable_months": joint_months,
                "latest_primary_reportable_month": "2025-02",
                "latest_secondary_reportable_month": "2025-02",
                "latest_joint_reportable_month": joint_latest,
                "latest_cached_month": "2025-02",
                "warnings": [],
            }
        if path.endswith("/reportable-months/refresh") and method == "POST":
            return {
                "status": self.refresh_status,
                "rows_saved": 4 if self.refresh_status == "updated" else 0,
                "warnings": ["operator detail that must not be shown"],
            }
        raise AssertionError(f"Unexpected request: {method} {path} {payload}")

    def run_async_and_poll(self, **kwargs):
        payload = deepcopy(kwargs.get("start_json"))
        self.run_payloads.append(payload)
        if self.reject_run:
            raise RuntimeError("submission rejected")
        return (
            "run-1",
            {"status": "completed"},
            {
                "run_id": "run-1",
                "country": "South Sudan",
                "time_period": payload["time_period"],
                "language": "en",
                "llm_calls": 0,
            },
        )

    @staticmethod
    def _saved_basket(role, version_id, payload):
        names = {1: "Maize", 2: "Beans", 3: "Salt"}
        return {
            "basket_version_id": version_id,
            "country_iso3": "SSD",
            "version_number": 3,
            "basket_role": role,
            "basket_name": payload["basket_name"],
            "short_description": payload.get("short_description") or "Primary MEB reference basket.",
            "scope_type": payload["scope_type"],
            "regions": list(payload.get("regions") or []),
            "status": "active",
            "created_at": "2026-07-02T10:00:00Z",
            "created_by_user_id": payload["created_by_user_id"],
            "cache_version_id_at_creation": "cache-v1",
            "change_note": payload.get("change_note"),
            "items": [
                {
                    "basket_item_id": f"saved-{item['commodity_id']}",
                    "basket_version_id": version_id,
                    "commodity_id": item["commodity_id"],
                    "commodity_name_snapshot": names[item["commodity_id"]],
                    "databridges_unit": "kg",
                    "weight_quantity": item["weight_quantity"],
                    "sort_order": index,
                    "item_note": item.get("item_note"),
                }
                for index, item in enumerate(payload["items"])
            ],
        }


def _element(elements, label):
    return next(element for element in elements if element.label == label)


def _app(monkeypatch, backend):
    # Importing the production streamlit_shared module inside ScriptRunner pulls
    # in every local backend and can deadlock Streamlit's AppTest import thread.
    # Install the page's narrow dependency surface before executing it instead.
    monkeypatch.setattr(builtins, "_price_bulletin_test_backend", backend, raising=False)
    page_path = str(PAGE).replace("\\", "\\\\")
    source = f'''
import builtins
import sys
import types
from urllib.parse import quote
import streamlit as st

backend = builtins._price_bulletin_test_backend
shared = types.ModuleType("streamlit_shared")
shared.apply_wfp_theme = lambda: None
shared.render_wfp_sidebar_logo = lambda: None
shared.render_onboarding_sidebar_button = lambda **kwargs: None
shared.render_instructions_sidebar_button = lambda **kwargs: None
shared.render_bug_report_sidebar_link = lambda **kwargs: None
shared.render_bug_report_header_link = lambda **kwargs: None
shared.render_report_delivery = lambda **kwargs: None
shared.render_report_blocks = lambda *args, **kwargs: None
shared.render_llm_diagnostics = lambda *args, **kwargs: None
shared.quote_path_param = lambda value: quote(str(value), safe="")
shared.request_json = backend.request_json
shared.request_bytes = lambda *args, **kwargs: b""
shared.run_async_and_poll = backend.run_async_and_poll
shared.safe_show_error = lambda error: st.error(str(error))
sys.modules["streamlit_shared"] = shared

page_path = r"{page_path}"
with open(page_path, encoding="utf-8") as page_file:
    exec(compile(page_file.read(), page_path, "exec"))
'''
    return AppTest.from_string(source).run(timeout=20)


def test_saved_cards_inclusion_and_id_based_additional_options(monkeypatch):
    backend = FakePriceBulletinBackend()
    app = _app(monkeypatch, backend)

    assert not app.exception
    include = _element(app.checkbox, "Include Urban basket in this report")
    assert include.value is True
    assert any("calculated independently" in info.value for info in app.info)
    assert any("Scope-specific second-basket charts are included" in info.value for info in app.info)
    assert backend.reportability_params[-1] == {
        "primary_basket_version_id": "primary-v1",
        "include_secondary_basket": True,
        "secondary_basket_version_id": "secondary-v1",
        "admin1_list": ["Juba", "Wau"],
    }
    additional = _element(app.multiselect, "Additional commodities")
    assert additional.options == ["Salt"]

    app = include.uncheck().run(timeout=20)
    additional = _element(app.multiselect, "Additional commodities")
    assert additional.options == ["Beans", "Salt"]
    assert not any("calculated independently" in info.value for info in app.info)
    assert backend.reportability_params[-1]["include_secondary_basket"] is False
    assert backend.reportability_params[-1]["secondary_basket_version_id"] is None


def test_cache_panel_and_news_date_controls_are_not_rendered(monkeypatch):
    backend = FakePriceBulletinBackend()
    app = _app(monkeypatch, backend)

    assert not app.exception
    assert not [box for box in app.checkbox if box.label == "Use News Dates"]
    assert not [field for field in app.date_input if field.label in {"News Start Date", "News End Date"}]
    assert not [metric for metric in app.metric if metric.label in {"Status", "Price Rows", "Cache version"}]
    assert not any("Price Cache" in item.value for item in app.markdown)


def test_joint_reportability_rollback_names_the_secondary(monkeypatch):
    backend = FakePriceBulletinBackend()
    backend.joint_latest_override = "2025-01"

    app = _app(monkeypatch, backend)

    assert any(
        "Including Urban basket changes the latest reportable month from 2025-02 to 2025-01" in info.value
        for info in app.info
    )


@pytest.mark.parametrize(
    ("refresh_status", "element_type", "expected"),
    [
        ("updated", "success", "DataBridges refresh succeeded: 4 new price rows were added."),
        ("no_update", "info", "DataBridges refresh succeeded but found nothing new."),
        ("unavailable", "warning", "DataBridges refresh failed. Please try again later."),
    ],
)
def test_refresh_outcomes_are_actionable(monkeypatch, refresh_status, element_type, expected):
    backend = FakePriceBulletinBackend()
    backend.refresh_status = refresh_status
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Refresh from DataBridges").click().run(timeout=20)

    elements = getattr(app, element_type)
    assert any(expected in element.value for element in elements)
    notices = list(app.info) + list(app.warning)
    assert not any("operator detail" in element.value for element in notices)


def test_edit_publish_cancel_and_archive_use_plural_routes(monkeypatch):
    backend = FakePriceBulletinBackend()
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Edit primary basket").click().run(timeout=20)
    app = _element(app.button, "Publish primary basket").click().run(timeout=20)
    primary_posts = [
        call for call in backend.calls if call[0] == "POST" and call[1].endswith("/baskets/primary")
    ]
    assert len(primary_posts) == 1
    assert primary_posts[0][2]["basket_role"] == "primary"
    assert "basket_name" in primary_posts[0][2]
    assert "scope_type" in primary_posts[0][2]

    app = _app(monkeypatch, backend)
    app = _element(app.button, "Edit second basket").click().run(timeout=20)
    app = _element(app.button, "Publish second basket").click().run(timeout=20)
    secondary_posts = [
        call for call in backend.calls if call[0] == "POST" and call[1].endswith("/baskets/secondary")
    ]
    assert len(secondary_posts) == 1
    assert secondary_posts[0][2]["basket_role"] == "secondary"

    app = _app(monkeypatch, backend)
    app = _element(app.button, "Edit second basket").click().run(timeout=20)
    app = _element(app.button, "Cancel").click().run(timeout=20)
    assert len([
        call for call in backend.calls if call[0] == "POST" and call[1].endswith("/baskets/secondary")
    ]) == 1

    app = _app(monkeypatch, backend)
    app = _element(app.button, "Remove second basket").click().run(timeout=20)
    app = _element(app.button, "Confirm removal").click().run(timeout=20)
    assert any(call[0] == "DELETE" and call[1].endswith("/baskets/secondary") for call in backend.calls)
    assert not [box for box in app.checkbox if box.label.startswith("Include ")]
    assert _element(app.button, "Add second basket")


def test_add_second_basket_cancel_keeps_configuration_absent(monkeypatch):
    backend = FakePriceBulletinBackend(secondary=False)
    app = _app(monkeypatch, backend)

    app = _element(app.button, "Add second basket").click().run(timeout=20)
    second_name_inputs = [field for field in app.text_input if field.label == "Basket name"]
    assert len(second_name_inputs) == 1
    assert second_name_inputs[0].value == ""
    app = _element(app.button, "Cancel").click().run(timeout=20)

    assert backend.secondary is None
    assert not [call for call in backend.calls if call[0] == "POST" and "/baskets/secondary" in call[1]]
    assert _element(app.button, "Add second basket")


def test_disjoint_secondary_scope_blocks_run_and_exclusion_pins_payload(monkeypatch):
    backend = FakePriceBulletinBackend()
    app = _app(monkeypatch, backend)

    app = _element(app.multiselect, "Regions (Admin1)").set_value(["Wau"]).run(timeout=20)
    app = _element(app.button, "Run").click().run(timeout=20)
    assert not backend.run_payloads
    assert any("no configured region" in error.value for error in app.error)

    include = _element(app.checkbox, "Include Urban basket in this report")
    app = include.uncheck().run(timeout=20)
    app = _element(app.multiselect, "Regions (Admin1)").set_value(["Wau"]).run(timeout=20)
    app = _element(app.button, "Run").click().run(timeout=20)

    assert len(backend.run_payloads) == 1
    payload = backend.run_payloads[0]
    assert payload["primary_basket_version_id"] == "primary-v1"
    assert payload["include_secondary_basket"] is False
    assert payload["secondary_basket_version_id"] is None
    assert "basket_version_id" not in payload
    assert "news_start_date" not in payload
    assert "news_end_date" not in payload
    assert _element(app.checkbox, "Include Urban basket in this report").value is True


def test_rejected_submission_preserves_unchecked_inclusion(monkeypatch):
    backend = FakePriceBulletinBackend()
    backend.reject_run = True
    app = _app(monkeypatch, backend)

    app = _element(app.checkbox, "Include Urban basket in this report").uncheck().run(timeout=20)
    app = _element(app.button, "Run").click().run(timeout=20)

    assert len(backend.run_payloads) == 1
    assert _element(app.checkbox, "Include Urban basket in this report").value is False
    assert any("submission rejected" in error.value for error in app.error)


def test_missing_primary_opens_default_editor_and_disables_generation(monkeypatch):
    backend = FakePriceBulletinBackend(primary=False, secondary=False)
    app = _app(monkeypatch, backend)

    assert _element(app.text_input, "Basket name").value == "MEB"
    assert _element(app.button, "Add second basket")
    assert not [button for button in app.button if button.label == "Run"]
    assert any("disabled until the primary" in info.value for info in app.info)


def test_disabled_second_basket_gate_hides_controls_and_sends_primary_only(monkeypatch):
    backend = FakePriceBulletinBackend(second_basket_enabled=False)
    app = _app(monkeypatch, backend)

    assert not app.exception
    assert not [box for box in app.checkbox if box.label.startswith("Include ")]
    assert not [button for button in app.button if "second basket" in button.label.lower()]
    assert any("temporarily disabled" in info.value for info in app.info)
    assert backend.secondary is not None
    assert backend.reportability_params[-1]["include_secondary_basket"] is False
    assert backend.reportability_params[-1]["secondary_basket_version_id"] is None
    assert _element(app.multiselect, "Additional commodities").options == ["Beans", "Salt"]

    app = _element(app.button, "Run").click().run(timeout=20)
    assert backend.run_payloads[-1]["include_secondary_basket"] is False
    assert backend.run_payloads[-1]["secondary_basket_version_id"] is None

    backend.second_basket_enabled = True
    app = app.run(timeout=20)
    assert _element(app.checkbox, "Include Urban basket in this report").value is True
