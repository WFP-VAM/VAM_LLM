from contextlib import nullcontext
from types import SimpleNamespace

from app.shared import async_runs
from app.services.market_monitor.price_backfill import (
    BasketReferenceMonthMissing,
    CommodityGapStatus,
    PriceRequirement,
    ReportPriceGapReport,
)
from app.streamlit_backend import dispatcher
import streamlit_shared
from streamlit_shared import ordered_live_output_sections


class ImmediateThread:
    def __init__(self, target=None, name=None, daemon=None, *args, **kwargs):
        self._target = target

    def start(self):
        if self._target is not None:
            self._target()


class FakeBasketSelection:
    def __init__(self, *, include_secondary=False):
        self.primary_basket_version_id = "active-basket"
        self.secondary_basket_version_id = "secondary-v1" if include_secondary else None
        self.secondary_basket_included = include_secondary

    def food_baskets_dict(self):
        return {
            "primary": {
                "basket_version_id": self.primary_basket_version_id,
                "basket_role": "primary",
                "basket_name": "MEB",
                "items": [],
            },
            "secondary": (
                {
                    "basket_version_id": self.secondary_basket_version_id,
                    "basket_role": "secondary",
                    "basket_name": "Pastoral Basket",
                    "items": [],
                }
                if self.secondary_basket_included
                else None
            ),
        }

    def to_metadata(self):
        return {
            "primary_basket_version_id": self.primary_basket_version_id,
            "secondary_basket_version_id": self.secondary_basket_version_id,
            "secondary_basket_included": self.secondary_basket_included,
            "food_baskets": self.food_baskets_dict(),
        }


def _reset_run_store(monkeypatch):
    monkeypatch.setattr(async_runs, "_BACKEND", "memory")
    async_runs._RUNS.clear()
    async_runs._RUN_ARTIFACTS.clear()


def test_market_monitor_async_status_exposes_live_outputs_and_artifacts(monkeypatch):
    _reset_run_store(monkeypatch)
    monkeypatch.setattr(
        dispatcher, "threading", SimpleNamespace(Thread=ImmediateThread)
    )
    selections = []
    graph_calls = []

    def fake_resolve(_country, **_kwargs):
        selection = FakeBasketSelection(include_secondary=True)
        selections.append(selection)
        return selection

    monkeypatch.setattr(dispatcher, "resolve_baskets_for_report", fake_resolve)

    def fake_run_report_generation(*, country, time_period, on_step=None, **kwargs):
        graph_calls.append(kwargs)
        if on_step is not None:
            on_step(
                "data_agent",
                {
                    "databridges_rows": [
                        {
                            "Country": country,
                            "Commodity": "Maize",
                            "Price Date": "2025-01-01T00:00:00.000Z",
                            "Price": 10.5,
                        }
                    ]
                },
            )
            on_step(
                "news_retrieval",
                {
                    "news_counts": {"Seerist": 1, "ReliefWeb": 1, "total": 2},
                    "retriever_traces": [
                        {"retriever": "ReliefWeb", "error": None},
                        {"retriever": "Seerist", "error": None},
                    ],
                    "seerist_documents": [
                        {
                            "doc_id": "seer-1",
                            "title": "Seerist title",
                            "url": "",
                            "source": "Seerist",
                            "date": "2025-01-10",
                            "content": "Seerist content",
                        }
                    ],
                    "reliefweb_documents": [
                        {
                            "doc_id": "rw-1",
                            "title": "ReliefWeb title",
                            "url": "https://reliefweb.test/report",
                            "source": "ReliefWeb",
                            "date": "2025-01-11",
                            "content": "ReliefWeb content",
                        }
                    ],
                },
            )
        return {
            "run_id": "final-market-run",
            "country": country,
            "time_period": time_period,
            "report_draft_sections": {},
            "visualizations": {},
            "data_statistics": {},
            "basket_statistics": {
                "primary": {"current_cost": 42},
                "secondary": {"current_cost": 21},
            },
            "basket_series_national": [
                {"Date": "2025-01-01", "BasketRole": "primary", "Cost": 42},
            ],
            "basket_series_regional": [],
            "cache_metadata": {
                "cache_version_id": "cache-v1",
                "basket_calculation_specs": [{"basket_role": "primary"}],
                "basket_applicable_regions": {"primary": []},
                "basket_coverage": {"primary": {"current_complete": True}},
            },
            "document_references": [],
            "news_counts": {"Seerist": 1, "ReliefWeb": 1, "total": 2},
            "warnings": [],
        }

    monkeypatch.setattr(dispatcher, "run_report_generation", fake_run_report_generation)

    response = dispatcher._market_monitor_generate_async(
        json_body={
            "country": "South Sudan",
            "time_period": "2025-01",
            "commodity_list": ["Maize"],
            "admin1_list": [],
            "currency_code": "SSP",
            "enabled_modules": [],
            "use_mock_data": False,
        }
    )
    run_id = response.json()["run_id"]

    run = async_runs.get_run(run_id)
    assert run is not None
    assert run.status == "completed"
    assert len(selections) == 2
    assert graph_calls[0]["basket_selection"] is selections[0]
    assert run.metadata["basket_selection"]["primary_basket_version_id"] == "active-basket"
    assert run.metadata["basket_selection"]["secondary_basket_included"] is True
    assert run.metadata["basket_calculation"]["cache_version_id"] == "cache-v1"
    assert run.metadata["basket_calculation"]["statistics"]["secondary"]["current_cost"] == 21
    assert run.result["food_baskets"]["secondary"]["basket_version_id"] == "secondary-v1"
    assert run.result["basket_statistics"]["secondary"]["current_cost"] == 21
    live_outputs = run.metadata["live_outputs"]
    assert live_outputs["databridges"]["rows_preview"][0]["Commodity"] == "Maize"
    assert live_outputs["seerist"]["documents"][0]["title"] == "Seerist title"
    assert live_outputs["reliefweb"]["documents"][0]["url"] == "https://reliefweb.test/report"

    artifact_path = live_outputs["databridges"]["download_artifacts"][0]["download_path"]
    artifact_response = dispatcher.dispatch_request("GET", artifact_path)
    assert artifact_response.status_code == 200
    assert artifact_response.headers["Content-Type"] == "application/json"

    result_response = dispatcher.dispatch_request(
        "GET",
        f"/market-monitor/result/{run_id}",
    )
    assert result_response.status_code == 200
    assert result_response.json()["secondary_basket_included"] is True
    assert result_response.json()["food_basket"]["basket_version_id"] == "active-basket"


def test_market_monitor_async_failure_stores_price_gap_report(monkeypatch):
    _reset_run_store(monkeypatch)
    monkeypatch.setattr(
        dispatcher, "threading", SimpleNamespace(Thread=ImmediateThread)
    )
    monkeypatch.setattr(
        dispatcher,
        "resolve_baskets_for_report",
        lambda _country, **_kwargs: FakeBasketSelection(),
    )
    gap_report = ReportPriceGapReport(
        country="Burkina Faso",
        iso3="BFA",
        time_period="2026-06",
        reference_month="2026-06",
        window_start="2025-06",
        window_end="2026-06",
        requirements=[
            PriceRequirement(
                commodity_id=52,
                commodity_name="Rice",
                month="2026-06",
                hard=True,
                is_basket=True,
                reason="reference_month_food_basket",
            )
        ],
        commodity_statuses=[
            CommodityGapStatus(
                commodity_id=52,
                commodity_name="Rice",
                is_basket=True,
                missing_reference_month=True,
                source_status="no_source_data",
                backfill_attempted=True,
            )
        ],
        backfill_attempted=True,
    )

    def fake_run_report_generation(**_kwargs):
        raise BasketReferenceMonthMissing(gap_report)

    monkeypatch.setattr(dispatcher, "run_report_generation", fake_run_report_generation)

    response = dispatcher._market_monitor_generate_async(
        json_body={
            "country": "Burkina Faso",
            "time_period": "2026-06",
            "commodity_list": ["Rice"],
            "admin1_list": [],
            "currency_code": "XOF",
            "enabled_modules": [],
            "use_mock_data": False,
        }
    )
    run = async_runs.get_run(response.json()["run_id"])

    assert run is not None
    assert run.status == "failed"
    assert run.error == str(BasketReferenceMonthMissing(gap_report))
    assert run.metadata["price_gap_report"]["hard_missing"][0]["commodity_id"] == 52


def test_market_monitor_dispatcher_routes_reportable_months(monkeypatch):
    monkeypatch.setattr(
        dispatcher,
        "get_reportable_months",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "cache_version_id": "cache-v1",
            "basket_version_id": "basket-v1",
            "reportable_months": ["2025-01", "2025-02"],
            "latest_reportable_month": "2025-02",
            "latest_cached_month": "2025-03",
            "latest_cached_real_month": "2025-02",
            "missing_by_month": {"2025-03": ["Beans"]},
            "warnings": [],
        },
    )

    response = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/reportable-months",
    )

    assert response.status_code == 200
    assert response.json()["country"] == "South Sudan"
    assert response.json()["latest_reportable_month"] == "2025-02"
    assert response.json()["missing_by_month"] == {"2025-03": ["Beans"]}


def test_market_monitor_dispatcher_forwards_two_basket_reportability_selection(monkeypatch):
    captured = {}

    def fake_reportable(country, **kwargs):
        captured.update({"country": country, **kwargs})
        return {"country": country, "iso3": "SSD", "joint_reportable_months": ["2025-01"]}

    monkeypatch.setattr(dispatcher, "get_reportable_months", fake_reportable)

    response = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/reportable-months",
        params={
            "primary_basket_version_id": "primary-v1",
            "include_secondary_basket": "true",
            "secondary_basket_version_id": "secondary-v1",
            "admin1_list": ["Juba", "Wau"],
        },
    )

    assert response.status_code == 200
    assert captured == {
        "country": "South Sudan",
        "basket_version_id": None,
        "primary_basket_version_id": "primary-v1",
        "include_secondary_basket": True,
        "secondary_basket_version_id": "secondary-v1",
        "admin1_list": ["Juba", "Wau"],
    }


def test_market_monitor_dispatcher_reportability_alias_conflict_is_422():
    response = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/reportable-months",
        params={"basket_version_id": "old", "primary_basket_version_id": "new"},
    )

    assert response.status_code == 422
    assert "must reference the same" in str(response.json()["detail"])


def test_market_monitor_dispatcher_routes_reportable_months_refresh(monkeypatch):
    captured = {}

    def fake_refresh(country, *, basket_version_id=None):
        captured["country"] = country
        captured["basket_version_id"] = basket_version_id
        return {
            "country": country,
            "iso3": "SSD",
            "status": "updated",
            "source_cache_version_id": "cache-v1",
            "new_cache_version_id": "cache-v2",
            "checked_start_month": "2025-03",
            "checked_end_month": "2025-03",
            "months_checked": ["2025-03"],
            "latest_reportable_month_before": "2025-02",
            "latest_reportable_month_after": "2025-03",
            "new_reportable_months": ["2025-03"],
            "rows_fetched": 2,
            "rows_real": 2,
            "rows_saved": 2,
            "rows_skipped_existing": 0,
            "excluded_non_real_rows": 0,
            "excluded_future_rows": 0,
            "deduplicated_rows": 0,
            "missing_by_month": {},
            "warnings": [],
        }

    monkeypatch.setattr(dispatcher, "refresh_reportable_months_from_databridges", fake_refresh)

    response = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/countries/South%20Sudan/reportable-months/refresh",
        json_body={"basket_version_id": "basket-v1"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "updated"
    assert captured == {"country": "South Sudan", "basket_version_id": "basket-v1"}


def test_market_monitor_dispatcher_refresh_forwards_two_basket_selection(monkeypatch):
    captured = {}

    def fake_refresh(country, **kwargs):
        captured.update({"country": country, **kwargs})
        return {"country": country, "iso3": "SSD", "status": "no_update", "warnings": []}

    monkeypatch.setattr(dispatcher, "refresh_reportable_months_from_databridges", fake_refresh)

    response = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/countries/South%20Sudan/reportable-months/refresh",
        json_body={
            "primary_basket_version_id": "primary-v1",
            "include_secondary_basket": True,
            "secondary_basket_version_id": "secondary-v1",
            "admin1_list": ["Juba"],
        },
    )

    assert response.status_code == 200
    assert captured == {
        "country": "South Sudan",
        "basket_version_id": None,
        "primary_basket_version_id": "primary-v1",
        "include_secondary_basket": True,
        "secondary_basket_version_id": "secondary-v1",
        "admin1_list": ["Juba"],
    }


def test_market_monitor_dispatcher_reportable_months_unavailable_returns_503(monkeypatch):
    def fake_reportable_months(_country):
        raise dispatcher.PriceCacheUnavailableError("cache unavailable")

    monkeypatch.setattr(dispatcher, "get_reportable_months", fake_reportable_months)

    response = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/reportable-months",
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "cache unavailable"


def test_market_monitor_dispatcher_reportable_months_refresh_conflict_returns_409(monkeypatch):
    def fake_refresh(_country, *, basket_version_id=None):
        raise dispatcher.BasketVersionConflict("refresh basket")

    monkeypatch.setattr(dispatcher, "refresh_reportable_months_from_databridges", fake_refresh)

    response = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/countries/South%20Sudan/reportable-months/refresh",
        json_body={"basket_version_id": "old"},
    )

    assert response.status_code == 409
    assert "refresh basket" in response.json()["detail"]


def test_market_monitor_dispatcher_routes_plural_basket_contract(monkeypatch):
    captured = []
    monkeypatch.setattr(
        dispatcher,
        "get_country_baskets_response",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "primary": {"basket_version_id": "primary-v1"},
            "secondary": {"basket_version_id": "secondary-v1"},
            "needs_primary_setup": False,
            "has_secondary": True,
        },
    )

    def fake_save(country, role, input_data):
        captured.append(("save", country, str(role), input_data.basket_name))
        return {
            "country": country,
            "iso3": "SSD",
            "primary": {"basket_version_id": "primary-v1"},
            "secondary": {"basket_version_id": "secondary-v1"},
            "needs_primary_setup": False,
            "has_secondary": True,
        }

    def fake_history(country, role, limit=20):
        captured.append(("history", country, str(role), limit))
        return {
            "country": country,
            "iso3": "SSD",
            "basket_role": str(role),
            "versions": [{"basket_version_id": "secondary-v1", "version_number": 2}],
        }

    monkeypatch.setattr(dispatcher, "save_country_basket_role", fake_save)
    monkeypatch.setattr(dispatcher, "list_country_basket_role_history", fake_history)
    monkeypatch.setattr(
        dispatcher,
        "archive_country_secondary_basket",
        lambda country: {
            "country": country,
            "iso3": "SSD",
            "primary": {"basket_version_id": "primary-v1"},
            "secondary": None,
            "needs_primary_setup": False,
            "has_secondary": False,
            "archived_secondary": {"basket_version_id": "secondary-v1", "status": "archived"},
        },
    )

    get_response = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/baskets",
    )
    save_response = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/countries/South%20Sudan/baskets/secondary",
        json_body={
            "basket_name": "Pastoral Basket",
            "short_description": "Pastoral household affordability proxy.",
            "items": [{"commodity_id": 2, "weight_quantity": 3}],
        },
    )
    history_response = dispatcher.dispatch_request(
        "GET",
        "/market-monitor/countries/South%20Sudan/baskets/secondary/history?limit=5",
    )
    archive_response = dispatcher.dispatch_request(
        "DELETE",
        "/market-monitor/countries/South%20Sudan/baskets/secondary",
    )

    assert get_response.status_code == 200
    assert save_response.status_code == 200
    assert history_response.status_code == 200
    assert archive_response.status_code == 200
    assert archive_response.json()["archived_secondary"]["status"] == "archived"
    assert captured == [
        ("save", "South Sudan", "secondary", "Pastoral Basket"),
        ("history", "South Sudan", "secondary", 5),
    ]


def test_market_monitor_dispatcher_generation_validation_matches_api(monkeypatch):
    response = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/generate",
        json_body={
            "country": "South Sudan",
            "time_period": "2025-02",
            "basket_version_id": "primary-v1",
            "primary_basket_version_id": "primary-v2",
        },
    )

    assert response.status_code == 422
    assert "must reference the same primary basket version" in response.text


def test_market_monitor_dispatcher_ignores_stale_secondary_when_excluded(monkeypatch):
    captured = {}

    def fake_resolve(country, **kwargs):
        captured["resolve"] = {"country": country, **kwargs}
        return FakeBasketSelection(include_secondary=False)

    monkeypatch.setattr(dispatcher, "resolve_baskets_for_report", fake_resolve)
    monkeypatch.setattr(
        dispatcher,
        "run_report_generation",
        lambda **kwargs: {
            "run_id": "dispatcher-sync",
            "country": kwargs["country"],
            "time_period": kwargs["time_period"],
            "report_draft_sections": {},
            "visualizations": {},
            "data_statistics": {"food_basket": {"current_price": 42}},
            "warnings": [],
        },
    )

    response = dispatcher.dispatch_request(
        "POST",
        "/market-monitor/generate",
        json_body={
            "country": "South Sudan",
            "time_period": "2025-02",
            "include_secondary_basket": False,
            "secondary_basket_version_id": "stale-secondary",
        },
    )

    assert response.status_code == 200
    assert captured["resolve"]["include_secondary_basket"] is False
    assert captured["resolve"]["secondary_basket_version_id"] == "stale-secondary"
    assert response.json()["food_baskets"]["secondary"] is None
    assert response.json()["secondary_basket_included"] is False


def test_mfi_dispatcher_routes_csv_endpoints(monkeypatch):
    calls = []

    monkeypatch.setattr(
        dispatcher,
        "_mfi_drafter_generate_from_csv",
        lambda **kwargs: calls.append(("generate", kwargs)) or dispatcher._json_response({"route": "generate"}),
    )
    monkeypatch.setattr(
        dispatcher,
        "_mfi_drafter_validate_csv",
        lambda **kwargs: calls.append(("validate", kwargs)) or dispatcher._json_response({"route": "validate"}),
    )
    monkeypatch.setattr(
        dispatcher,
        "_mfi_drafter_generate_from_csv_async",
        lambda **kwargs: calls.append(("generate_async", kwargs)) or dispatcher._json_response({"route": "generate_async"}),
    )

    files = {"file": object()}
    data = {"country_override": "Sudan"}

    generate_response = dispatcher.dispatch_request("POST", "/mfi-drafter/generate-from-csv", data=data, files=files)
    validate_response = dispatcher.dispatch_request("POST", "/mfi-drafter/validate-csv", files=files)
    async_response = dispatcher.dispatch_request("POST", "/mfi-drafter/generate-from-csv-async", data=data, files=files)

    assert generate_response.json()["route"] == "generate"
    assert validate_response.json()["route"] == "validate"
    assert async_response.json()["route"] == "generate_async"
    assert [name for name, _kwargs in calls] == ["generate", "validate", "generate_async"]


def test_mfi_dispatcher_removed_survey_endpoints_return_404():
    survey_response = dispatcher.dispatch_request("POST", "/mfi-drafter/generate-from-survey", json_body={"survey_id": 123})
    surveys_response = dispatcher.dispatch_request("GET", "/mfi-drafter/countries/South%20Sudan/surveys")

    assert survey_response.status_code == 404
    assert surveys_response.status_code == 404
    assert "Unknown MFI drafter endpoint" in survey_response.json()["detail"]
    assert "Unknown MFI drafter endpoint" in surveys_response.json()["detail"]


def test_mfi_dispatcher_info_advertises_csv_upload_and_the_light_workflow():
    from app.services.mfi_drafter.light_contracts import NODES

    info = dispatcher.dispatch_request("GET", "/mfi-drafter/info").json()

    assert info["supports_csv_upload"] is True
    assert info["data_source"] == "Uploaded processed MFI CSV"
    assert info["csv_upload"]["endpoint"] == "/generate-from-csv"
    assert "inputs" not in info
    assert "databridges" not in info
    assert [node["id"] for node in info["workflow_nodes"]] == list(NODES)


def test_run_async_and_poll_enables_downloads_only_for_final_status(monkeypatch):
    events = []

    class DummyPlaceholder:
        def container(self):
            return nullcontext()

    responses = iter(
        [
            {"run_id": "mfi_test"},
            {"run_id": "mfi_test", "status": "running", "progress_pct": 25},
            {"run_id": "mfi_test", "status": "completed", "progress_pct": 100},
            {"success": True},
        ]
    )

    monkeypatch.setattr(streamlit_shared.st, "empty", lambda: DummyPlaceholder())
    monkeypatch.setattr(streamlit_shared, "request_json", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(streamlit_shared.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        streamlit_shared,
        "render_run_status",
        lambda status, **kwargs: events.append((status.get("status"), kwargs.get("render_instance_id"), kwargs.get("enable_downloads"))),
    )

    run_id, final_status, result = streamlit_shared.run_async_and_poll(
        start_method="POST",
        start_path="/start",
        status_path_template="/status/{run_id}",
        result_path_template="/result/{run_id}",
        poll_interval_seconds=0.0,
        timeout_seconds=1,
    )

    assert run_id == "mfi_test"
    assert final_status["status"] == "completed"
    assert result == {"success": True}
    assert events == [
        ("running", "poll-0", False),
        ("completed", None, True),
    ]


def test_ordered_live_output_sections_prioritizes_standard_order():
    ordered = ordered_live_output_sections(
        {
            "reliefweb": {"kind": "documents"},
            "extra": {"kind": "unknown"},
            "seerist": {"kind": "documents"},
            "databridges": {"kind": "table"},
        }
    )

    assert [name for name, _payload in ordered] == ["databridges", "seerist", "reliefweb", "extra"]
