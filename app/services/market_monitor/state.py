"""The state of a Market Monitor run: its fields, its initial value, and the report language and currency it carries."""
from __future__ import annotations

import uuid
from typing import TypedDict, Annotated, List, Dict, Any, Optional, Mapping

import operator


class MarketReportState(TypedDict):
    """Stato principale del grafo."""
    
    # ===== INPUTS =====
    country: str
    time_period: str
    commodity_list: List[str]
    basket_version_id: Optional[str]
    primary_basket_version_id: Optional[str]
    include_secondary_basket: bool
    secondary_basket_version_id: Optional[str]
    admin1_list: List[str]
    previous_report_text: str
    currency_code: str
    use_mock_data: bool
    language: str
    locale: str
    language_source: str
    
    # ===== MODULE CONFIG =====
    enabled_modules: List[str]
    
    # ===== BRANCH 1 OUTPUTS (Data & Graphs) =====
    time_series_data_national: Optional[str]  # JSON
    time_series_data_regional: Optional[str]  # JSON
    time_series_history_national: Optional[str]  # JSON
    data_statistics: Optional[Dict[str, Any]]
    databridges_rows: List[Dict[str, Any]]
    cache_metadata: Dict[str, Any]
    food_basket: Dict[str, Any]
    food_baskets: Dict[str, Any]
    basket_series_national: List[Dict[str, Any]]
    basket_series_regional: List[Dict[str, Any]]
    basket_statistics: Dict[str, Any]
    visualizations: Dict[str, str]  # Base64 images

    # ===== BRANCH 2 OUTPUTS (Contextual Intelligence) =====
    documents: List[Dict[str, Any]]
    document_references: List[Dict[str, Any]]
    seerist_documents: List[Dict[str, Any]]
    reliefweb_documents: List[Dict[str, Any]]
    news_counts: Dict[str, int]
    retriever_traces: List[Dict[str, Any]]
    events: List[Dict[str, Any]]
    trend_analysis: Optional[Dict[str, Any]]

    # ===== MODULE OUTPUTS =====
    exchange_rate_data: Optional[Dict[str, Any]]
    fuel_energy_data: Optional[Dict[str, Any]]
    livestock_animal_products_data: Optional[Dict[str, Any]]
    labour_market_data: Optional[Dict[str, Any]]
    module_sections: Dict[str, str]
    
    # ===== CENTRAL & QA OUTPUTS =====
    report_draft_sections: Dict[str, str]
    skeptic_flags: List[Dict[str, Any]]
    qa_review: Dict[str, Any]
    correction_targets: List[str]

    # ===== CONTROL & METADATA =====
    warnings: Annotated[List[str], operator.add]
    run_id: str
    correction_attempts: int
    llm_calls: int
    llm_diagnostics: Dict[str, Any]
    current_node: str


def create_initial_state(
    country: str,
    time_period: str,
    commodity_list: List[str],
    admin1_list: List[str],
    currency_code: str,
    enabled_modules: List[str],
    basket_version_id: Optional[str] = None,
    basket_selection: Optional[Mapping[str, Any]] = None,
    previous_report_text: str = "",
    use_mock_data: bool = False,
    language: str = "en",
    locale: str = "en_US",
    language_source: str = "default",
    run_id: Optional[str] = None,
) -> MarketReportState:
    """Crea stato iniziale per il grafo."""
    selection = dict(basket_selection or {})
    food_baskets = dict(selection.get("food_baskets") or {})
    primary_snapshot = food_baskets.get("primary")
    secondary_snapshot = food_baskets.get("secondary")
    primary_version_id = selection.get("primary_basket_version_id") or basket_version_id
    secondary_version_id = selection.get("secondary_basket_version_id")
    secondary_included = bool(selection.get("secondary_basket_included", False))
    return MarketReportState(
        country=country,
        time_period=time_period,
        commodity_list=commodity_list,
        basket_version_id=basket_version_id,
        primary_basket_version_id=primary_version_id,
        include_secondary_basket=secondary_included,
        secondary_basket_version_id=secondary_version_id,
        admin1_list=admin1_list,
        previous_report_text=previous_report_text,
        currency_code=currency_code,
        use_mock_data=use_mock_data,
        language=language,
        locale=locale,
        language_source=language_source,
        enabled_modules=enabled_modules,
        time_series_data_national=None,
        time_series_data_regional=None,
        time_series_history_national=None,
        data_statistics=None,
        databridges_rows=[],
        cache_metadata={},
        food_basket=dict(primary_snapshot or {}),
        food_baskets={
            "primary": primary_snapshot,
            "secondary": secondary_snapshot if secondary_included else None,
        },
        basket_series_national=[],
        basket_series_regional=[],
        basket_statistics={"primary": None, "secondary": None},
        visualizations={},
        documents=[],
        document_references=[],
        seerist_documents=[],
        reliefweb_documents=[],
        news_counts={"Seerist": 0, "ReliefWeb": 0, "total": 0},
        retriever_traces=[],
        events=[],
        trend_analysis=None,
        exchange_rate_data=None,
        fuel_energy_data=None,
        livestock_animal_products_data=None,
        labour_market_data=None,
        module_sections={},
        report_draft_sections={},
        skeptic_flags=[],
        qa_review={"status": "not_recorded", "correction_attempts": 0, "flags": []},
        correction_targets=[],
        warnings=[],
        run_id=run_id or f"run_{uuid.uuid4().hex[:8]}",
        correction_attempts=0,
        llm_calls=0,
        llm_diagnostics={},
        current_node="init"
    )


def _state_language(state: Dict[str, Any]) -> str:
    return str(state.get("language") or "en").strip().lower() or "en"


def _state_currency_code(state: Dict[str, Any]) -> str:
    cache_metadata = state.get("cache_metadata") or {}
    code = cache_metadata.get("currency_code") or state.get("currency_code") or "LCU"
    code = str(code or "LCU").strip().upper()
    return code or "LCU"
