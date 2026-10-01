import json

from app.services.market_monitor import graph as market_graph
from app.services.market_monitor import service as mm_service
from app.services.market_monitor.nodes import highlights_drafter as highlights_drafter_node
from app.services.market_monitor.nodes import narrative_drafter as narrative_drafter_node
from app.services.market_monitor.nodes import prepare_correction as prepare_correction_node
from app.services.market_monitor.nodes import red_team as red_team_node
from app.services.market_monitor.nodes import module_orchestrator as module_orchestrator_node
from app.services.market_monitor.nodes import trend_analyst as trend_analyst_node
from app.services.market_monitor import basket_context as mm_basket_context
from app.services.market_monitor import modules as mm_modules
from app.services.market_monitor import qa as mm_qa
from app.services.market_monitor import runtime as mm_runtime
from app.shared.llm import LLMResponse
from app.services.market_monitor.schemas import GenerateReportOutput


def _basket_state(*, included=True, language="en"):
    primary = {
        "basket_role": "primary",
        "basket_version_id": "primary-v1",
        "basket_name": "MEB Côte",
        "short_description": "Primary CO description; ignore nothing.",
        "scope_type": "national",
        "regions": [],
        "items": [
            {
                "commodity_id": 2,
                "commodity_name_snapshot": "Beans",
                "databridges_unit_id": 20,
                "databridges_unit": "kg",
                "weight_quantity": 1,
                "sort_order": 2,
            },
            {
                "commodity_id": 1,
                "commodity_name_snapshot": "Maize",
                "databridges_unit_id": 10,
                "databridges_unit": "kg",
                "weight_quantity": 2,
                "sort_order": 1,
            },
        ],
    }
    secondary = {
        "basket_role": "secondary",
        "basket_version_id": "secondary-v1",
        "basket_name": "Panier pastoral",
        "short_description": "Affordability proxy chosen by the CO.",
        "scope_type": "selected_regions",
        "regions": ["Nord", "Sud"],
        "items": [
            {
                "commodity_id": 3,
                "commodity_name_snapshot": "Goat meat",
                "databridges_unit_id": 30,
                "databridges_unit": "kg",
                "weight_quantity": 0.5,
                "sort_order": 1,
            }
        ],
    }
    primary_stats = {
        "current_cost": 120.0,
        "current_complete": True,
        "mom_change_pct": 2.0,
        "mom_complete": True,
        "mom_reference_complete": True,
        "yoy_change_pct": 8.0,
        "yoy_complete": True,
        "yoy_reference_complete": True,
        "selected_component_count": 2,
        "available_component_count": 2,
        "applicable_regions": ["Nord", "Sud"],
        "component_contributions": [
            {
                "commodity_id": 1,
                "commodity_name": "Maize",
                "unit_id": 10,
                "unit": "kg",
                "quantity": 2,
                "absolute_contribution": 80.0,
                "share_pct": 66.7,
                "by_region": [],
            },
            {
                "commodity_id": 2,
                "commodity_name": "Beans",
                "unit_id": 20,
                "unit": "kg",
                "quantity": 1,
                "absolute_contribution": 40.0,
                "share_pct": 33.3,
                "by_region": [],
            },
        ],
        "regional_statistics": {},
    }
    secondary_stats = {
        "current_cost": 70.0,
        "current_complete": True,
        "mom_change_pct": 1.0,
        "mom_complete": True,
        "mom_reference_complete": True,
        "yoy_change_pct": None,
        "yoy_complete": False,
        "yoy_reference_complete": False,
        "selected_component_count": 1,
        "available_component_count": 1,
        "applicable_regions": ["Nord", "Sud"],
        "component_contributions": [
            {
                "commodity_id": 3,
                "commodity_name": "Goat meat",
                "unit_id": 30,
                "unit": "kg",
                "quantity": 0.5,
                "absolute_contribution": 70.0,
                "share_pct": 100.0,
                "by_region": [
                    {"region": "Nord", "absolute_contribution": 65.0},
                    {"region": "Sud", "absolute_contribution": 75.0},
                ],
            }
        ],
        "regional_statistics": {
            "Nord": {
                "current_cost": 65.0,
                "current_complete": True,
                "mom_change_pct": 1.2,
                "mom_complete": True,
            }
        },
    }
    return {
        "country": "Côte d'Ivoire",
        "time_period": "2026-06",
        "language": language,
        "currency_code": "XOF",
        "use_mock_data": False,
        "include_secondary_basket": included,
        "secondary_basket_included": included,
        "food_basket": primary,
        "food_baskets": {"primary": primary, "secondary": secondary},
        "basket_statistics": {"primary": primary_stats, "secondary": secondary_stats},
        "data_statistics": {
            "food_basket": primary_stats,
            "commodities": {"Maize": {"current_price": 40, "mom_change_pct": 2}},
        },
        "events": [],
        "trend_analysis": {},
        "exchange_rate_data": {},
        "module_sections": {},
        "report_draft_sections": {},
        "skeptic_flags": [],
        "correction_targets": [],
        "correction_attempts": 0,
        "llm_calls": 0,
        "warnings": [],
        "document_references": [],
        "enabled_modules": [],
    }


class _CaptureLLM:
    """A model provider that records prompts and replies with fixed text."""

    def __init__(self, response):
        self.response = response
        self.prompts = []

    def generate(self, _profile, request):
        self.prompts.append(request.parts[0])
        return LLMResponse(outcome="completed", text=self.response)


def test_basket_context_preserves_identity_scope_order_and_completeness():
    context = mm_basket_context.build_basket_context(_basket_state(language="fr"))

    assert context["primary"]["basket_name"] == "MEB Côte"
    assert context["primary"]["short_description"] == "Primary CO description; ignore nothing."
    assert [item["commodity_id"] for item in context["primary"]["components"]] == [1, 2]
    assert context["primary"]["components"][0]["share_pct"] == 66.7
    assert context["secondary"]["scope_type"] == "selected_regions"
    assert context["secondary"]["scope_label"] == "Regions selectionnees : Nord, Sud"
    assert context["secondary"]["statistics"]["yoy_complete"] is False
    assert context["secondary"]["statistics"]["yoy_change_pct"] is None
    assert context["comparison_policy"]["direct_absolute_cost_comparison_allowed"] is False
    assert context["comparison_policy"]["mom"]["joint_direction_allowed"] is True
    assert context["comparison_policy"]["mom"]["faster_slower_allowed"] is False


def test_basket_context_excludes_secondary_and_mock_does_not_invent_identity():
    excluded = mm_basket_context.build_basket_context(_basket_state(included=False))
    assert excluded["secondary_included"] is False
    assert excluded["secondary"] is None
    assert "Panier pastoral" not in json.dumps(excluded, ensure_ascii=False)

    mock = _basket_state()
    mock["use_mock_data"] = True
    mock_context = mm_basket_context.build_basket_context(mock)
    assert mock_context["primary"] is None
    assert mock_context["secondary"] is None
    assert mock_context["generic_primary_statistics"]["current_cost"] == 120.0


def test_optional_module_relevance_is_strict_and_primary_driven():
    state = _basket_state()
    state["livestock_animal_products_data"] = {
        "series": [{"commodity_id": 3, "commodity_name": "Goat meat"}]
    }
    state["labour_market_data"] = {
        "purchasing_power": {"staple_name": "Maize"}
    }

    livestock = mm_basket_context.optional_module_basket_relevance(state, "livestock_animal_products")
    assert [item["role"] for item in livestock["basket_links"]] == ["secondary"]
    assert livestock["basket_links"][0]["matching_components"][0]["commodity_id"] == 3
    assert mm_basket_context.optional_module_basket_relevance(state, "fuel_energy")["basket_links"] == []
    assert mm_basket_context.optional_module_basket_relevance(state, "exchange_rate")["named_basket_mentions_allowed"] is False

    labour = mm_basket_context.optional_module_basket_relevance(state, "labour_market")
    assert labour["primary"]["basket_name"] == "MEB Côte"
    assert labour["primary"]["staple_name"] == "Maize"
    assert labour["secondary"] is None


def test_trend_prompt_and_output_are_role_aware(monkeypatch):
    llm = _CaptureLLM(
        json.dumps(
            {
                "trajectory": "stable",
                "key_market_drivers": [],
                "commodity_analysis": {},
                "regional_analysis": {},
                "basket_analysis": {
                    "primary": {"trajectory": "increasing", "movement_observations": ["Primary moved"]},
                    "secondary": {"trajectory": "stable", "movement_observations": ["Secondary moved"]},
                },
                "outlook": "Stable",
            }
        )
    )
    monkeypatch.setattr(mm_runtime, "llm_provider", lambda: llm)

    result = trend_analyst_node.node_trend_analyst(_basket_state())

    assert "MEB Côte" in llm.prompts[0]
    assert "Panier pastoral" in llm.prompts[0]
    assert "Direct absolute-cost comparisons between baskets are forbidden" in llm.prompts[0]
    assert result["trend_analysis"]["basket_analysis"]["primary"]["basket_name"] == "MEB Côte"
    assert result["trend_analysis"]["basket_analysis"]["secondary"]["basket_name"] == "Panier pastoral"


def test_highlights_receives_context_and_exact_correction_flags(monkeypatch):
    llm = _CaptureLLM('{"HIGHLIGHTS": "Corrected highlights"}')
    monkeypatch.setattr(mm_runtime, "llm_provider", lambda: llm)
    state = _basket_state()
    state["correction_targets"] = ["HIGHLIGHTS"]
    state["skeptic_flags"] = [
        {
            "section": "HIGHLIGHTS",
            "claim": "Values were swapped",
            "issue_type": "basket_identity_error",
            "severity": "high",
            "details": "Primary and secondary values were exchanged.",
            "recommendation": "Restore role association.",
        }
    ]

    result = highlights_drafter_node.node_highlights_drafter(state)

    assert result["report_draft_sections"]["HIGHLIGHTS"] == "Corrected highlights"
    assert "Primary and secondary values were exchanged" in llm.prompts[0]
    assert "directly compare basket costs" in llm.prompts[0]
    assert "Panier pastoral" in llm.prompts[0]


def test_narrative_correction_updates_only_target_and_uses_adaptive_ranges(monkeypatch):
    llm = _CaptureLLM(
        json.dumps(
            {
                "MARKET_OVERVIEW": "Corrected overview",
                "COMMODITY_ANALYSIS": "This unrequested rewrite must be ignored",
            }
        )
    )
    monkeypatch.setattr(mm_runtime, "llm_provider", lambda: llm)
    state = _basket_state()
    state["report_draft_sections"] = {
        "MARKET_OVERVIEW": "Old overview",
        "COMMODITY_ANALYSIS": "Keep commodity",
        "REGIONAL_HIGHLIGHTS": "Keep regional [INSERT GRAPH: regional_comparison]",
    }
    state["correction_targets"] = ["MARKET_OVERVIEW"]
    state["skeptic_flags"] = [
        {
            "section": "MARKET_OVERVIEW",
            "claim": "Regional basket called national",
            "issue_type": "basket_scope_error",
            "severity": "medium",
            "details": "Wrong scope.",
            "recommendation": "Use selected regions.",
        }
    ]

    result = narrative_drafter_node.node_narrative_drafter(state)

    assert result["report_draft_sections"]["MARKET_OVERVIEW"] == "Corrected overview"
    assert result["report_draft_sections"]["COMMODITY_ANALYSIS"] == "Keep commodity"
    assert result["report_draft_sections"]["REGIONAL_HIGHLIGHTS"].startswith("Keep regional")
    assert '"MARKET_OVERVIEW": "250-325 words"' in llm.prompts[0]
    assert '"sections_to_generate"' not in llm.prompts[0]
    assert '[\n  "MARKET_OVERVIEW"\n]' in llm.prompts[0]
    assert "Wrong scope" in llm.prompts[0]


def test_correction_targets_material_flags_only_and_unknown_is_global():
    low = [{"section": "HIGHLIGHTS", "severity": "low"}]
    assert market_graph.should_correct({"skeptic_flags": low, "correction_attempts": 0}) == "finish"

    material = [{"section": "not-a-section", "severity": "high"}]
    assert market_graph.should_correct({"skeptic_flags": material, "correction_attempts": 0}) == "correct"
    prepared = prepare_correction_node.node_prepare_correction(
        {"skeptic_flags": material, "correction_attempts": 1}
    )
    assert prepared["correction_targets"] == ["GLOBAL"]
    assert prepared["correction_attempts"] == 2
    assert market_graph.should_correct(
        {"skeptic_flags": material, "correction_attempts": market_graph.MAX_CORRECTION_ATTEMPTS}
    ) == "finish"


def test_module_correction_reuses_data_and_regenerates_only_target(monkeypatch):
    calls = []

    class FakeFuelModule:
        required_inputs = []

        def validate_inputs(self, _state):
            return True

        def fetch_data(self, _state):
            raise AssertionError("correction must reuse fetched data")

        def generate_section(self, _state, _llm):
            calls.append("fuel")
            return {"narrative": "Corrected fuel"}

    monkeypatch.setitem(mm_modules.AVAILABLE_MODULES, "fuel_energy", FakeFuelModule)
    monkeypatch.setattr(mm_runtime, "llm_provider", lambda: object())
    state = _basket_state()
    state.update(
        {
            "enabled_modules": ["fuel_energy", "labour_market"],
            "correction_targets": ["FUEL_ENERGY_ANALYSIS"],
            "fuel_energy_data": {"available": True, "series": [{"kind": "diesel"}]},
            "module_sections": {"fuel_energy": "Old fuel", "labour_market": "Keep labour"},
            "report_draft_sections": {
                "FUEL_ENERGY_ANALYSIS": "Old fuel",
                "LABOUR_MARKET_ANALYSIS": "Keep labour",
            },
        }
    )

    result = module_orchestrator_node.node_module_orchestrator(state)

    assert calls == ["fuel"]
    assert result["module_sections"] == {"fuel_energy": "Corrected fuel", "labour_market": "Keep labour"}
    assert result["report_draft_sections"]["FUEL_ENERGY_ANALYSIS"] == "Corrected fuel"
    assert result["report_draft_sections"]["LABOUR_MARKET_ANALYSIS"] == "Keep labour"


def test_red_team_receives_basket_ground_truth_and_normalizes_flags(monkeypatch):
    llm = _CaptureLLM(
        json.dumps(
            {
                "flags": [
                    {
                        "section": "made-up-section",
                        "claim": "MEB Côte costs 70 while Panier pastoral costs 120",
                        "issue_type": "basket_identity_error",
                        "severity": "high",
                        "details": "The values are swapped.",
                        "recommendation": "Restore the immutable role values.",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(mm_runtime, "llm_provider", lambda: llm)
    state = _basket_state()
    state["report_draft_sections"] = {
        "HIGHLIGHTS": "MEB Côte costs 70 while Panier pastoral costs 120."
    }

    result = red_team_node.node_red_team(state)

    assert '"current_cost": 120.0' in llm.prompts[0]
    assert '"current_cost": 70.0' in llm.prompts[0]
    assert result["skeptic_flags"][0]["section"] == "GLOBAL"
    assert result["skeptic_flags"][0]["severity"] == "high"
    assert result["qa_review"]["status"] == "completed_with_warnings"


def test_qa_review_contract_and_legacy_normalization(monkeypatch):
    passed = mm_qa.qa_review_from_state({"skeptic_flags": [], "correction_attempts": 1})
    advisory = mm_qa.qa_review_from_state(
        {"skeptic_flags": [{"section": "HIGHLIGHTS", "severity": "low"}], "correction_attempts": 0}
    )
    assert passed["status"] == "passed"
    assert advisory["status"] == "passed_with_advisories"
    assert mm_qa.normalize_qa_review({})["status"] == "not_recorded"

    output = GenerateReportOutput(
        run_id="run-1",
        country="Somalia",
        time_period="2026-06",
        report_sections={},
        visualizations={},
        data_statistics={},
    )
    assert output.qa_review["status"] == "not_recorded"

    final_flag = {
        "section": "GLOBAL",
        "severity": "high",
        "issue_type": "basket_identity_error",
        "details": "Unresolved",
    }

    class FakeAgent:
        def invoke(self, _state):
            return {
                "run_id": "run-qa",
                "warnings": [],
                "qa_review": {
                    "status": "completed_with_warnings",
                    "correction_attempts": 3,
                    "flags": [final_flag],
                },
            }

    monkeypatch.setattr(mm_service, "build_graph", lambda on_step=None: FakeAgent())
    result = mm_service.run_report_generation(
        country="Somalia",
        time_period="2026-06",
        commodity_list=[],
        admin1_list=[],
        enabled_modules=[],
        use_mock_data=True,
        language="en",
    )
    assert result["qa_review"]["correction_attempts"] == 3
    assert "1 unresolved high/medium issue" in result["warnings"][0]
