"""
MFI Drafter - Schemas
=====================
Classi e modelli per la generazione di MFI Reports.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, List, Dict, Any, Literal

from app.shared.report_blocks import ReportBlock
from app.shared.llm_observability import LLMRunDiagnostics
from .methodology import ANALYSIS_SCHEMA_VERSION, DISPLAY_DIMENSIONS


# ============================================================================
# CONSTANTS
# ============================================================================

MFI_DIMENSIONS = list(DISPLAY_DIMENSIONS)


def get_risk_level(mfi_score: float) -> str:
    """Classifica il livello di rischio in base allo score MFI."""
    if mfi_score < 4.0:
        return "Very High Risk"
    elif mfi_score < 5.5:
        return "High Risk"
    elif mfi_score < 7.0:
        return "Medium Risk"
    else:
        return "Low Risk"


# ============================================================================
# DATA CLASSES
# ============================================================================


# ============================================================================
# MFI 2.0 METHODOLOGY EVIDENCE
# ============================================================================

class MFIMetric(BaseModel):
    applicability: Literal["applicable", "not_applicable", "not_represented", "unknown"] = "unknown"
    """One exact processed-DataBridge metric for one assessed market."""

    metric_id: str
    parsing_status: str = "valid"
    applicability_basis: str = "observed"
    source_rows: List[int] = Field(default_factory=list)
    source_values: List[Optional[str]] = Field(default_factory=list)
    dimension: str
    display_name: str
    variable_name: str
    source_level_id: int
    source_level_name: str
    role: Literal[
        "official_score",
        "official_subsection",
        "dimension_validation_component",
        "question_driver",
        "category_driver",
        "item_driver",
    ]
    raw_value: Optional[float] = None
    raw_min: float
    raw_max: float
    normalized_value: Optional[float] = None
    orientation: Literal["higher_is_better", "higher_is_worse", "descriptive"]
    unit: Literal["score", "proportion"]
    evidence_scope: Literal["assessed_market", "surveyed_traders_in_market"]
    observed_raw_values: List[Optional[float]] = Field(default_factory=list)
    market_coverage: int = 0
    market_coverage_total: int = 0
    missing_count: int = 0
    applicability_status: Literal[
        "available",
        "missing",
        "not_applicable",
        "not_represented",
    ] = "available"
    validation_status: Literal[
        "valid",
        "missing",
        "out_of_range",
        "duplicate",
        "formula_mismatch",
    ] = "valid"
    methodology_note: str
    product_group: Optional[str] = None
    question_group: Optional[str] = None
    item_name: Optional[str] = None
    severity_weight: Optional[int] = None
    traders_sample_size: Optional[int] = None


class MFIMetricSummary(BaseModel):
    """Deterministic unweighted summary across available assessed markets."""

    metric_id: str
    dimension: str
    display_name: str
    role: str
    mean_raw_value: Optional[float] = None
    mean_normalized_value: Optional[float] = None
    aggregation_numerator: Optional[float] = None
    aggregation_denominator: int = 0
    available_market_count: int = 0
    total_assessed_market_count: int = 0
    missing_count: int = 0
    applicability_counts: Dict[str, int] = Field(default_factory=dict)
    unit: str
    orientation: str
    evidence_scope: str
    contributing_metric_ids: List[str] = Field(default_factory=list)
    methodology_note: str = ""


class MFIMethodologyWarning(BaseModel):
    """Structured, user-visible methodology or coverage warning."""

    code: str
    severity: Literal["warning", "error"] = "warning"
    message: str
    market_name: Optional[str] = None
    dimension: Optional[str] = None
    metric_ids: List[str] = Field(default_factory=list)
    expected_value: Optional[float] = None
    actual_value: Optional[float] = None
    delta: Optional[float] = None
    tolerance: Optional[float] = None


class MFIExcludedMarketRecord(BaseModel):
    """A processed market record excluded from the Full MFI assessment."""

    market_name: str
    detected_record_type: Literal["mfir_only"]
    reason: str
    available_level1_variables: List[str] = Field(default_factory=list)
    missing_level1_variables: List[str] = Field(default_factory=list)


# ============================================================================
# MFI 2.0 DETERMINISTIC ANALYSIS
# ============================================================================

class MFIAnalysisConfig(BaseModel):
    """Injectable, validated configuration for the pure Phase 2 analysis."""

    model_config = ConfigDict(frozen=True)

    priority_dimension_min: int = 3
    priority_dimension_max: int = 4
    priority_market_max: int = 15
    market_weak_dimension_count: int = 3
    item_min_market_count: int = 3
    item_min_market_ratio: float = 0.25
    item_category_contrast: float = 0.10
    item_max_per_group: int = 3
    ranking_tie_tolerance: float = 1e-6
    quartile_interpolation: Literal["linear"] = "linear"
    #: Required evidence must reach this share of assessed markets to pass without a
    #: warning. The default treats any shortfall in required evidence as material, which
    #: is safe because optional partial representation is classified separately.
    partial_required_warning_ratio: float = 1.0

    @model_validator(mode="after")
    def validate_analysis_config(self) -> "MFIAnalysisConfig":
        if self.priority_dimension_min < 1:
            raise ValueError("priority_dimension_min must be at least 1")
        if self.priority_dimension_max < self.priority_dimension_min:
            raise ValueError(
                "priority_dimension_max must be greater than or equal to "
                "priority_dimension_min"
            )
        if self.priority_dimension_max > len(MFI_DIMENSIONS):
            raise ValueError("priority_dimension_max cannot exceed the dimension count")
        if self.priority_market_max < 1:
            raise ValueError("priority_market_max must be at least 1")
        if not 1 <= self.market_weak_dimension_count <= len(MFI_DIMENSIONS):
            raise ValueError("market_weak_dimension_count is outside the dimension count")
        if self.item_min_market_count < 1:
            raise ValueError("item_min_market_count must be at least 1")
        if not 0.0 <= self.item_min_market_ratio <= 1.0:
            raise ValueError("item_min_market_ratio must be between 0 and 1")
        if not 0.0 <= self.item_category_contrast <= 1.0:
            raise ValueError("item_category_contrast must be between 0 and 1")
        if self.item_max_per_group < 1:
            raise ValueError("item_max_per_group must be at least 1")
        if self.ranking_tie_tolerance < 0.0:
            raise ValueError("ranking_tie_tolerance cannot be negative")
        if not 0.0 <= self.partial_required_warning_ratio <= 1.0:
            raise ValueError(
                "partial_required_warning_ratio must be between 0 and 1"
            )
        return self


class MFICoverageSummary(BaseModel):
    """Assessment-level market coverage for one deterministic value."""

    available_market_count: int
    total_assessed_market_count: int
    missing_count: int
    coverage_ratio: float


class MFIEvidenceAvailability(BaseModel):
    """Why a metric's evidence is incomplete, and whether that warrants a warning.

    Market coverage alone cannot distinguish a required subsection that failed to load
    from an optional item that simply was not sold in every market. Both are "fewer
    markets than assessed", but only the first is a methodology problem. This
    classification carries that distinction so warnings can be raised for genuine
    evidence failures and coverage can be disclosed neutrally for everything else.
    """

    classification: Literal[
        "complete",
        "unknown_applicability",
        "partial_required",
        "partial_optional",
        "not_applicable",
        "unusable_required",
    ]
    applicability_rule: Literal[
        "required",
        "optional_product_group",
        "optional_item",
        "quality_applicability",
    ] = "required"
    role: str = ""
    represented_market_count: int = 0
    total_assessed_market_count: int = 0
    invalid_market_count: int = 0
    #: True only for classifications that represent a genuine evidence failure.
    warrants_warning: bool = False

    @property
    def is_optional(self) -> bool:
        return self.applicability_rule in {"optional_item", "optional_product_group"}


class MFIStatisticalSummary(BaseModel):
    """Unrounded unweighted statistics over included assessed markets."""

    mean: float
    median: float
    minimum: float
    maximum: float
    q1: float
    q3: float
    iqr: float
    score_range: float
    numerator: float
    denominator: int
    coverage: MFICoverageSummary


class MFIRankedValue(BaseModel):
    """One value in a deterministic, tolerance-aware ordered population."""

    name: str
    value: float
    rank: int
    selection_order: int
    ledger_metric_id: str


class MFIAnalyzedMetric(BaseModel):
    """Assessment-level subsection or driver with deterministic ranking."""

    metric_id: str
    dimension: str
    display_name: str
    role: str
    mean_raw_value: Optional[float] = None
    mean_normalized_value: Optional[float] = None
    unit: str
    orientation: str
    evidence_scope: str
    coverage: MFICoverageSummary
    availability: Optional[MFIEvidenceAvailability] = None
    unfavorable_rate: Optional[float] = None
    weakness_rank: Optional[int] = None
    group_rank: Optional[int] = None
    product_group: Optional[str] = None
    question_group: Optional[str] = None
    item_name: Optional[str] = None
    severity_weight: Optional[int] = None
    item_relevant: bool = False
    relevance_reasons: List[str] = Field(default_factory=list)
    matching_category_metric_id: Optional[str] = None
    source_metric_ids: List[str] = Field(default_factory=list)
    ledger_metric_ids: List[str] = Field(default_factory=list)


class MFIRegionalDimensionSummary(BaseModel):
    """One dimension's unweighted summary and rank inside one region."""

    region: str
    statistics: MFIStatisticalSummary
    rank: int
    selection_order: int
    ledger_metric_ids: List[str] = Field(default_factory=list)


class MFILocalizedPatterns(BaseModel):
    """Deterministic location patterns for one dimension."""

    regions_where_bottom_one: List[str] = Field(default_factory=list)
    regions_where_bottom_two: List[str] = Field(default_factory=list)
    markets_where_lowest: List[str] = Field(default_factory=list)
    ordered_markets: List[MFIRankedValue] = Field(default_factory=list)
    score_range: float
    iqr: float


class MFIDimensionProfile(BaseModel):
    """Complete deterministic analytical profile for one official dimension."""

    dimension: str
    workflow_revision: Optional[str] = None
    analytical_facts: Dict[str, Any] = Field(default_factory=dict)
    statistics: MFIStatisticalSummary
    profile_rank: int
    selection_order: int
    is_priority: bool
    priority_reasons: List[
        Literal["bottom_rank", "below_profile_mean"]
    ] = Field(default_factory=list)
    subsections: List[MFIAnalyzedMetric] = Field(default_factory=list)
    drivers: List[MFIAnalyzedMetric] = Field(default_factory=list)
    regional_summaries: List[MFIRegionalDimensionSummary] = Field(default_factory=list)
    localized_patterns: MFILocalizedPatterns
    ledger_metric_ids: List[str] = Field(default_factory=list)


class MFIMarketDimensionProfile(BaseModel):
    """One official dimension score and weakness status inside a market."""

    dimension: str
    score: float
    rank: int
    selection_order: int
    is_weak: bool
    ledger_metric_ids: List[str] = Field(default_factory=list)


class MFIMarketProfile(BaseModel):
    """Stored overall score, relative rank, and weak dimensions for one market."""

    market_name: str
    market_key: Optional[str] = None
    region: Optional[str] = None
    overall_mfi: float
    score_rank: int
    selection_order: int
    is_priority_market: bool
    selection_reasons: List[str] = Field(default_factory=list)
    weak_dimensions: List[MFIMarketDimensionProfile] = Field(default_factory=list)
    dimension_profile: List[MFIMarketDimensionProfile] = Field(default_factory=list)
    ledger_metric_ids: List[str] = Field(default_factory=list)


class MFILimitation(BaseModel):
    """Stable, explicit limitation attached to the deterministic profile."""

    code: str
    severity: Literal["info", "warning"] = "warning"
    message: str
    market_name: Optional[str] = None
    region: Optional[str] = None
    dimension: Optional[str] = None
    metric_ids: List[str] = Field(default_factory=list)


MFIAggregationMethod = Literal[
    "unweighted_market_mean",
    "market_value",
    "rank",
    "count",
    "coverage",
]
MFIPopulationBasis = Literal[
    "market_level",
    "trader_level_within_market",
    "descriptive",
]
MFIRepresentationBasis = Literal[
    "all_assessed_markets",
    "represented_assessed_markets",
    "applicable_assessed_markets",
    "incomplete_assessed_markets",
    "single_assessed_market",
    "assessment_dimension_profile",
    "assessment_input_records",
]


class MFILedgerSemantics(BaseModel):
    """Explicit aggregation semantics for a ledger entry that cannot be derived.

    Every field is optional and overrides the derived value field by field, so a call
    site can correct one aspect without restating the rest.
    """

    aggregation_method: Optional[MFIAggregationMethod] = None
    population_basis: Optional[MFIPopulationBasis] = None
    pooled_denominator_available: Optional[bool] = None
    representation_basis: Optional[MFIRepresentationBasis] = None
    permitted_subject_phrase: Optional[str] = None


class MFIMetricLedgerEntry(BaseModel):
    """One uniquely addressable value supporting profiles and tables.

    The aggregation fields record *what population a value describes*, which market
    coverage alone cannot express. An assessment-wide mean of per-market trader rates and
    a single market's trader proportion are both proportions over the same underlying
    question, but only the second has a respondent denominator. Recording the difference
    here is what allows correct wording to be required later.
    """

    ledger_id: str
    label: str
    value: float
    statistic: str
    unit: str
    orientation: str
    evidence_scope: str
    dimension: Optional[str] = None
    market_name: Optional[str] = None
    region: Optional[str] = None
    coverage: Optional[MFICoverageSummary] = None
    source_metric_ids: List[str] = Field(default_factory=list)
    #: How per-unit values were combined into this number.
    aggregation_method: MFIAggregationMethod = "unweighted_market_mean"
    #: The elementary unit of observation behind this number's denominator.
    population_basis: MFIPopulationBasis = "descriptive"
    #: Whether a denominator for this value exists in the processed data and may
    #: therefore be stated numerically. False for every trader-level value, because the
    #: assessment carries no applicability-specific respondent counts.
    pooled_denominator_available: bool = False
    #: Which assessed markets stand behind the value.
    representation_basis: MFIRepresentationBasis = "all_assessed_markets"
    #: Deterministic noun phrase describing the value correctly. Never contains a digit,
    #: so quoting it can never introduce an unauthorized numeric token.
    permitted_subject_phrase: str = ""


class MFIDeterministicTableRow(BaseModel):
    """A presentation-neutral table row backed entirely by ledger entries."""

    row_id: str
    values: Dict[str, Any] = Field(default_factory=dict)
    ledger_metric_ids: List[str] = Field(default_factory=list)


class MFIDeterministicTables(BaseModel):
    """Versioned, deterministic tables ready for later presentation work."""

    dimension_rows: List[MFIDeterministicTableRow] = Field(default_factory=list)
    regional_rows: List[MFIDeterministicTableRow] = Field(default_factory=list)
    subsection_rows: List[MFIDeterministicTableRow] = Field(default_factory=list)
    driver_rows: List[MFIDeterministicTableRow] = Field(default_factory=list)
    relevant_item_rows: List[MFIDeterministicTableRow] = Field(default_factory=list)
    priority_market_rows: List[MFIDeterministicTableRow] = Field(default_factory=list)


class MFIAssessmentProfile(BaseModel):
    """Complete public Phase 2 deterministic assessment profile."""

    analysis_schema_version: Literal["2.0", "2.1"] = ANALYSIS_SCHEMA_VERSION
    workflow_revision: Optional[str] = None
    analytical_facts: Dict[str, Any] = Field(default_factory=dict)
    coverage_manifest: List[Dict[str, Any]] = Field(default_factory=list)
    market_identities: Dict[str, Any] = Field(default_factory=dict)
    analysis_version: str
    methodology_version: str
    score_authority: str
    assessed_market_count: int
    excluded_market_count: int = 0
    mean_mfi_across_assessed_markets: float
    overall_statistics: MFIStatisticalSummary
    dimension_profile_mean: float
    dimensions: List[MFIDimensionProfile]
    markets: List[MFIMarketProfile]
    priority_dimension_names: List[str]
    priority_market_names: List[str]
    limitations: List[MFILimitation] = Field(default_factory=list)
    metric_ledger: Dict[str, MFIMetricLedgerEntry] = Field(default_factory=dict)
    tables: MFIDeterministicTables


# ============================================================================
# MFI 2.0 STRUCTURED NARRATIVE AND QA
# ============================================================================


MFIContextRetrieverState = Literal[
    "completed",
    "no_results",
    "failed",
    "not_attempted",
]
MFIContextOverallState = Literal[
    "available",
    "no_results",
    "retrieval_failed",
    "classification_failed",
    "no_accepted_statements",
    "not_attempted",
]
MFIContextLimitationCode = Literal[
    "context_retrieval_unavailable",
    "context_partial_retrieval_unavailable",
    "context_classification_unavailable",
    "context_partial_classification_unavailable",
]


class MFIContextRetrieverStatus(BaseModel):
    """Stable public outcome for one contextual-document provider."""

    model_config = ConfigDict(frozen=True)

    status: MFIContextRetrieverState
    retrieved_document_count: int = Field(ge=0)


class MFIContextStatus(BaseModel):
    """Deterministic, provider-independent context availability disclosure."""

    model_config = ConfigDict(frozen=True)

    status: MFIContextOverallState
    retrievers: Dict[str, MFIContextRetrieverStatus] = Field(default_factory=dict)
    total_deduplicated_documents_retrieved: int = Field(ge=0)
    statements_classified: int = Field(ge=0)
    final_accepted_statements: int = Field(ge=0)
    extraction_mode: Literal[
        "not_started",
        "llm",
        "fallback",
        "failed",
        "not_applicable",
        "offline",
    ] = "not_started"
    limitation_code: Optional[MFIContextLimitationCode] = None
    classification_outcome: Literal["not_started", "completed", "degraded", "failed"] = "not_started"
    unresolved_statement_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_context_status(self) -> "MFIContextStatus":
        ordered = {
            key: self.retrievers[key]
            for key in sorted(self.retrievers, key=str.casefold)
        }
        object.__setattr__(self, "retrievers", ordered)
        retrieved_sum = sum(
            item.retrieved_document_count for item in ordered.values()
        )
        if retrieved_sum != self.total_deduplicated_documents_retrieved:
            raise ValueError(
                "total_deduplicated_documents_retrieved must equal the sum of "
                "per-source retrieved_document_count values"
            )
        if self.final_accepted_statements > self.statements_classified:
            raise ValueError(
                "final_accepted_statements cannot exceed statements_classified"
            )
        failed_sources = sum(item.status == "failed" for item in ordered.values())
        expected_limitation: Optional[str] = None
        if self.status == "available" and (
            self.final_accepted_statements < 1
            or self.total_deduplicated_documents_retrieved < 1
        ):
            raise ValueError(
                "available context requires a retrieved document and accepted statement"
            )
        if self.status == "no_results":
            if self.total_deduplicated_documents_retrieved or failed_sources:
                raise ValueError("no_results requires zero documents and no failed source")
            if self.statements_classified:
                raise ValueError("no_results cannot contain classified statements")
        elif self.status == "retrieval_failed":
            if self.total_deduplicated_documents_retrieved or not failed_sources:
                raise ValueError(
                    "retrieval_failed requires zero documents and a failed source"
                )
            if self.statements_classified:
                raise ValueError("retrieval_failed cannot contain classified statements")
            expected_limitation = "context_retrieval_unavailable"
        elif self.status == "classification_failed":
            if self.total_deduplicated_documents_retrieved < 1:
                raise ValueError("classification_failed requires retrieved documents")
            if self.final_accepted_statements:
                raise ValueError(
                    "classification_failed cannot contain accepted statements"
                )
            expected_limitation = "context_classification_unavailable"
        elif self.status == "no_accepted_statements":
            if self.total_deduplicated_documents_retrieved < 1:
                raise ValueError(
                    "no_accepted_statements requires retrieved documents"
                )
            if self.final_accepted_statements:
                raise ValueError(
                    "no_accepted_statements cannot contain accepted statements"
                )
        elif self.status == "not_attempted":
            if self.total_deduplicated_documents_retrieved:
                raise ValueError("not_attempted cannot contain retrieved documents")
            if self.statements_classified or any(
                item.status != "not_attempted" for item in ordered.values()
            ):
                raise ValueError(
                    "not_attempted requires every source and classifier to be unattempted"
                )
        if (
            expected_limitation is None
            and failed_sources
            and self.total_deduplicated_documents_retrieved
        ):
            expected_limitation = "context_partial_retrieval_unavailable"
        if self.unresolved_statement_count:
            if self.classification_outcome != "degraded" or not self.total_deduplicated_documents_retrieved:
                raise ValueError("Unresolved classification requires a degraded result and retrieved documents")
            expected_limitation = "context_partial_classification_unavailable"
        if self.limitation_code != expected_limitation:
            raise ValueError(
                "limitation_code is inconsistent with the context outcome"
            )
        return self


class MFIReleaseControl(BaseModel):
    """Immutable deployment-control snapshot attached to an MFI run."""

    analysis_version: str
    enabled: bool
    configuration_status: Literal[
        "configured",
        "default_disabled",
        "invalid",
    ]
    service_name: str = "mfi-drafter"
    deployment_revision: Optional[str] = None


# ============================================================================
# PYDANTIC MODELS (API)
# ============================================================================

class LightMFIReportOutput(BaseModel):
    """Section-level reports do not claim legacy per-claim QA certification."""
    model_config = ConfigDict(extra="allow")
    run_id: str
    workflow_revision: Literal["mfi-light-v1"]
    narrative_schema_version: Literal["3.0"]
    analysis_schema_version: Literal["2.1"]
    country: str
    light_narrative: Dict[str, Any]
    review_reports: Dict[str, Any]
    report_blocks: List[ReportBlock]
    generation_diagnostics: Dict[str, Any]
    llm_diagnostics: LLMRunDiagnostics
    success: bool = True


class MFIReportStatusOutput(BaseModel):
    """Status of an in-progress report; phase progress is in metadata.generation_diagnostics."""
    run_id: str
    status: Literal["pending", "running", "completed", "failed"]
    current_node: Optional[str] = None
    progress_pct: int = 0
    warnings: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    traceback: Optional[str] = None
