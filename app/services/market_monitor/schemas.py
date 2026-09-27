"""
Market Monitor - Schemas
========================
Classi e modelli per la generazione di Market Monitor Reports.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator
from app.shared.llm import LLMRunDiagnostics
from typing import Optional, List, Dict, Any, Literal
from dataclasses import dataclass, field, asdict

from app.shared.documents.blocks import ReportBlock


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class ModuleOutput:
    """Output standardizzato di un modulo opzionale."""
    section_title: str
    narrative: str
    visualization: Optional[str] = None  # Base64 encoded image
    key_metrics: Dict[str, Any] = field(default_factory=dict)
    data_for_qa: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Document:
    """Documento recuperato da fonti esterne."""
    doc_id: str
    title: str
    url: str
    source: str
    date: str
    content: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    """Evento estratto dai documenti."""
    event_id: str
    category: Literal["political", "economic", "climate", "security", "logistics", "agriculture", "other"]
    statement: str
    source_ids: List[str]
    location: str
    date: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrendAnalysis:
    """Analisi del trend di mercato."""
    trajectory: Literal["increasing_prices", "decreasing_prices", "stable", "volatile"]
    key_market_drivers: List[str]
    commodity_analysis: Dict[str, str]
    regional_analysis: Dict[str, str]
    outlook: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SkepticFlag:
    """Flag sollevato dal Red Team QA."""
    section: str
    claim: str
    issue_type: Literal["numeracy_error", "contradiction", "source_mismatch", 
                        "unsupported_speculation", "hedging", "template_violation"]
    severity: Literal["high", "medium", "low"]
    details: str
    recommendation: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DataStatistics:
    """Statistiche calcolate sui dati."""
    food_basket: Dict[str, Any] = field(default_factory=dict)
    commodities: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    auxiliary: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# PYDANTIC MODELS (API)
# ============================================================================

class BasketConfigurationOutput(BaseModel):
    country: str
    iso3: str
    primary: Optional[Dict[str, Any]] = None
    secondary: Optional[Dict[str, Any]] = None
    needs_primary_setup: bool
    has_secondary: bool
    second_basket_enabled: bool = True


class BasketArchiveOutput(BasketConfigurationOutput):
    archived_secondary: Optional[Dict[str, Any]] = None


class BasketHistoryOutput(BaseModel):
    country: str
    iso3: str
    basket_role: Literal["primary", "secondary"]
    versions: List[Dict[str, Any]] = Field(default_factory=list)


class ReportableMonthsInput(BaseModel):
    """Immutable basket and region selection used for reportability and refresh."""

    basket_version_id: Optional[str] = Field(
        default=None,
        description="Deprecated primary basket version alias.",
    )
    primary_basket_version_id: Optional[str] = None
    include_secondary_basket: bool = Field(
        default=False,
        description="Reportability remains primary-only unless secondary inclusion is explicit.",
    )
    secondary_basket_version_id: Optional[str] = None
    admin1_list: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_basket_version_aliases(self) -> "ReportableMonthsInput":
        legacy = _optional_identifier(self.basket_version_id)
        primary = _optional_identifier(self.primary_basket_version_id)
        secondary = _optional_identifier(self.secondary_basket_version_id)
        if legacy and primary and legacy != primary:
            raise ValueError(
                "basket_version_id and primary_basket_version_id must reference the same primary basket version."
            )
        self.basket_version_id = legacy
        self.primary_basket_version_id = primary
        self.secondary_basket_version_id = secondary
        self.admin1_list = [str(item).strip() for item in self.admin1_list if str(item).strip()]
        return self

    @property
    def effective_primary_basket_version_id(self) -> Optional[str]:
        return self.primary_basket_version_id or self.basket_version_id


class GenerateReportInput(BaseModel):
    """Input for generating a report."""
    country: str = Field(..., description="Country name (e.g., 'Sudan', 'Yemen')")
    time_period: str = Field(..., description="Period in YYYY-MM format (e.g., '2025-01')")
    language: Literal["auto", "en", "fr", "es"] = Field(
        default="auto",
        description="Report language: auto country default, English, French, or Spanish."
    )

    commodity_list: List[str] = Field(
        default=[],
        description="Additional commodities to analyze. Active basket commodities are always included."
    )
    basket_version_id: Optional[str] = Field(
        default=None,
        description="Deprecated alias for primary_basket_version_id. Stale versions are rejected."
    )
    primary_basket_version_id: Optional[str] = Field(
        default=None,
        description="Active primary basket version selected by the UI. Stale versions are rejected."
    )
    include_secondary_basket: bool = Field(
        default=True,
        description="Include the active secondary basket in this report when one exists."
    )
    secondary_basket_version_id: Optional[str] = Field(
        default=None,
        description="Active secondary basket version selected by the UI. Ignored when inclusion is false."
    )
    admin1_list: List[str] = Field(
        default=[],
        description="List of Admin1 regions to include"
    )
    currency_code: str = Field(
        default="USD",
        description="ISO 4217 currency code (e.g., 'SDG', 'YER')"
    )
    enabled_modules: List[str] = Field(
        default=[],
        description=(
            "Optional modules to enable "
            "(exchange_rate uses DataBridges FX first, fuel_energy uses transport fuel price series, "
            "livestock_animal_products uses animal product price series, labour_market uses wage series)"
        )
    )
    previous_report_text: str = Field(
        default="",
        description="Previous report text for context"
    )

    use_mock_data: bool = Field(
        default=False,
        description="If True, use mock numeric datasets instead of real APIs (Seerist/ReliefWeb news retrieval is always real)"
    )

    @model_validator(mode="after")
    def validate_basket_version_aliases(self) -> "GenerateReportInput":
        legacy = _optional_identifier(self.basket_version_id)
        primary = _optional_identifier(self.primary_basket_version_id)
        secondary = _optional_identifier(self.secondary_basket_version_id)
        if legacy and primary and legacy != primary:
            raise ValueError(
                "basket_version_id and primary_basket_version_id must reference the same primary basket version."
            )
        self.basket_version_id = legacy
        self.primary_basket_version_id = primary
        self.secondary_basket_version_id = secondary
        return self

    @property
    def effective_primary_basket_version_id(self) -> Optional[str]:
        return self.primary_basket_version_id or self.basket_version_id


class GenerateReportOutput(BaseModel):
    """Output of the report generation."""
    run_id: str
    country: str
    time_period: str
    language: str = "en"
    locale: str = "en_US"
    language_source: str = "default"
    report_sections: Dict[str, str]
    report_blocks: List[ReportBlock] = []
    visualizations: Dict[str, str]  # Base64 encoded images
    data_statistics: Dict[str, Any]
    trend_analysis: Optional[Dict[str, Any]] = None
    events: List[Dict[str, Any]] = []
    module_sections: Dict[str, str] = {}
    document_references: List[Dict[str, Any]] = []
    news_counts: Dict[str, int] = {}
    cache_metadata: Dict[str, Any] = {}
    food_basket: Dict[str, Any] = {}
    food_baskets: Dict[str, Any] = Field(
        default_factory=lambda: {"primary": None, "secondary": None}
    )
    basket_statistics: Dict[str, Any] = Field(
        default_factory=lambda: {"primary": None, "secondary": None}
    )
    basket_series_national: List[Dict[str, Any]] = Field(default_factory=list)
    basket_series_regional: List[Dict[str, Any]] = Field(default_factory=list)
    secondary_basket_included: bool = False
    qa_review: Dict[str, Any] = Field(
        default_factory=lambda: {"status": "not_recorded", "correction_attempts": 0, "flags": []}
    )
    fuel_energy_data: Optional[Dict[str, Any]] = None
    livestock_animal_products_data: Optional[Dict[str, Any]] = None
    labour_market_data: Optional[Dict[str, Any]] = None
    warnings: List[str] = []
    llm_calls: int = 0
    llm_diagnostics: LLMRunDiagnostics = Field(
        default_factory=lambda: LLMRunDiagnostics(
            service="market-monitor",
            run_id="unknown",
        )
    )
    success: bool = True


def _optional_identifier(value: Optional[str]) -> Optional[str]:
    if value in (None, ""):
        return None
    normalized = str(value).strip()
    return normalized or None


class ReportStatusOutput(BaseModel):
    """Status of an in-progress report."""
    run_id: str
    status: Literal["pending", "running", "completed", "failed", "interrupted"]
    current_node: Optional[str] = None
    progress_pct: int = 0
    warnings: List[str] = []
    metadata: Dict[str, Any] = {}
    error: Optional[str] = None
    traceback: Optional[str] = None
