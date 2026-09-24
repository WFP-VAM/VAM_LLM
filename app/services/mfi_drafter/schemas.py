"""
MFI Drafter - Schemas
=====================
Classi e modelli per la generazione di MFI Reports.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Optional, List, Dict, Any, Literal
from dataclasses import dataclass, asdict

from app.shared.report_blocks import ReportBlock
from app.shared.llm_observability import LLMRunDiagnostics
from .methodology import (
    ANALYSIS_SCHEMA_VERSION,
    CSV_DIMENSION_TO_DISPLAY,
    DISPLAY_DIMENSIONS,
    METHODOLOGY_VERSION,
    NARRATIVE_SCHEMA_VERSION,
    OFFICIAL_DIMENSION_SCORE_VARIABLES,
    SCORE_AUTHORITY,
)
from .reliable_contracts import NarrativeSegment, SourcePassage
from .table_projection import MFIReportTableColumn, MFIReportTableSpec


# ============================================================================
# CONSTANTS
# ============================================================================

MFI_DIMENSIONS = list(DISPLAY_DIMENSIONS)

RISK_COLORS = {
    "Very High Risk": "#d62728",
    "High Risk": "#ff7f0e",
    "Medium Risk": "#ffbb78",
    "Low Risk": "#2ca02c"
}

DIMENSION_NAME_MAP = {
    key: value
    for key, value in CSV_DIMENSION_TO_DISPLAY.items()
    if key != "MFI"
}

SCORE_VARIABLE_MAP = {
    **dict(OFFICIAL_DIMENSION_SCORE_VARIABLES),
    "MFI": "MFIScoreMFI",
}


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
class MFIMarketData:
    """Dati MFI per un singolo mercato."""
    market_name: str
    admin0: str
    admin1: str
    admin2: str
    region: str
    overall_mfi: float
    dimension_scores: Dict[str, float]
    subsections: Dict[str, List[Dict[str, Any]]]
    drivers: Dict[str, List[Dict[str, Any]]]
    risk_level: str
    traders_surveyed: int
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MFIDimensionScore:
    """Aggregazione score per dimensione."""
    dimension: str
    national_score: float
    regional_scores: Dict[str, float]
    market_scores: Dict[str, float]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DimensionFinding:
    """Findings generati per una dimensione."""
    key_findings: str
    score_interpretation: str
    recommendations: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SkepticFlag:
    """Flag sollevato dal Red Team QA."""
    section: str
    claim: str
    issue_type: Literal["score_mismatch", "interpretation_error", 
                        "template_violation", "missing_content"]
    severity: Literal["high", "medium", "low"]
    details: str
    recommendation: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SurveyMetadata:
    """Metadati del survey MFI."""
    country: str
    collection_period: str
    total_traders: int
    total_markets: int
    regions_covered: List[str]

    def to_dict(self) -> dict:
        return asdict(self)


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

class MFIContextEvidenceStatement(BaseModel):
    withdrawn: bool = False
    passage_binding_required: bool = False
    source_passages: List[Dict[str, Any]] = Field(default_factory=list)
    """One source-linked contextual statement classified before drafting."""

    statement_id: str
    text: str
    classification: Literal[
        "corroborating",
        "potentially_explanatory",
        "unrelated",
    ]
    document_ids: List[str] = Field(default_factory=list)
    validation_status: Literal["pending", "verified", "unverified"] = "pending"
    validation_flags: List[str] = Field(default_factory=list)
    validation_flag_ids: List[str] = Field(default_factory=list)
    substituted: bool = False


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


class MFIClaimCatalogEntry(BaseModel):
    """Closed deterministic value that narrative claims may cite."""

    metric_id: str
    fact: Optional[Dict[str, Any]] = None
    label: str
    numeric_value: float
    formatted_value: str
    allowed_renderings: List[str] = Field(default_factory=list)
    statistic: str
    unit: str
    orientation: str
    scope: str
    dimension: Optional[str] = None
    market_name: Optional[str] = None
    region: Optional[str] = None
    coverage_label: Optional[str] = None
    source_metric_ids: List[str] = Field(default_factory=list)
    aggregation_method: MFIAggregationMethod = "unweighted_market_mean"
    population_basis: MFIPopulationBasis = "descriptive"
    pooled_denominator_available: bool = False
    representation_basis: MFIRepresentationBasis = "all_assessed_markets"
    permitted_subject_phrase: str = ""
    #: Claim scopes this value can legitimately support. Kept separate from ``scope`` so
    #: that a value can back more than one, which a single canonical scope cannot express.
    permitted_claim_scopes: List[str] = Field(default_factory=list)
    #: Representation counts as integers, so a claim can cite the denominator without
    #: parsing it back out of the formatted coverage label.
    represented_market_count: Optional[int] = None
    assessed_market_count: Optional[int] = None
    representation_kind: Literal[
        "fixed_metric",
        "item",
        "applicability",
        "none",
    ] = "none"
    representation_complete: bool = False
    representation_required: bool = False


class MFINarrativeClaim(BaseModel):
    """One independently cited and validated narrative statement."""

    claim_id: str
    segments: List[Dict[str, Any]] = Field(default_factory=list)
    fact_ids: List[str] = Field(default_factory=list)
    source_passages: List[Dict[str, Any]] = Field(default_factory=list)
    revision: int = 1
    text: str
    claim_kind: Literal[
        "summary",
        "finding",
        "geographic_pattern",
        "limitation",
        "recommendation",
        "context",
        "modality_consideration",
    ]
    metric_ids: List[str] = Field(default_factory=list)
    document_ids: List[str] = Field(default_factory=list)
    scope: Literal[
        "assessment",
        "region",
        "market",
        "surveyed_traders",
        "context",
    ] = "assessment"
    polarity: Literal[
        "favorable",
        "unfavorable",
        "neutral",
        "descriptive",
    ] = "neutral"
    validation_status: Literal["pending", "verified", "unverified"] = "pending"
    #: Flag codes, which are stable across runs and safe to display.
    validation_flags: List[str] = Field(default_factory=list)
    #: Flag identifiers, which hash the message and therefore change whenever wording
    #: does. Kept separate from the codes so a consumer can pick the stable one.
    validation_flag_ids: List[str] = Field(default_factory=list)
    #: True when the drafted text was withdrawn and replaced by deterministic wording.
    substituted: bool = False


class MFISubdimensionNarrative(BaseModel):
    """Priority-dimension interpretation linked to official evidence."""

    name: str
    subsection_metric_id: Optional[str] = None
    score_0_10: Optional[float] = None
    interpretation: MFINarrativeClaim
    driver_metric_ids: List[str] = Field(default_factory=list)


class MFIDimensionNarrative(BaseModel):
    """Canonical structured narrative for one MFI dimension."""

    dimension: str
    is_priority: bool
    summary: MFINarrativeClaim
    key_findings: List[MFINarrativeClaim] = Field(default_factory=list)
    subdimension_analysis: List[MFISubdimensionNarrative] = Field(
        default_factory=list
    )
    geographic_patterns: List[MFINarrativeClaim] = Field(default_factory=list)
    data_limitations: List[MFINarrativeClaim] = Field(default_factory=list)
    recommendations: List[MFINarrativeClaim] = Field(default_factory=list)


class MFIMarketNarrative(BaseModel):
    """Canonical recommendation narrative for one selected market."""

    market_name: str
    market_key: Optional[str] = None
    region: Optional[str] = None
    overall_mfi: float
    score_rank: int
    weak_dimensions: List[str] = Field(default_factory=list)
    priority_issues: List[MFINarrativeClaim] = Field(default_factory=list)
    recommended_interventions: List[MFINarrativeClaim] = Field(
        default_factory=list
    )
    limitations: List[MFINarrativeClaim] = Field(
        default_factory=list,
        max_length=1,
        description=(
            "Optional market-relevant, metric-cited limitation. Generic assessment "
            "limitations belong in the executive and methodology sections."
        ),
    )
    modality_consideration: Optional[MFINarrativeClaim] = None


class MFIExecutiveNarrative(BaseModel):
    """Canonical structured executive summary."""

    motivation: Optional[MFINarrativeClaim] = None
    key_findings: List[MFINarrativeClaim] = Field(default_factory=list)
    recommendations: List[MFINarrativeClaim] = Field(default_factory=list)
    limitations: List[MFINarrativeClaim] = Field(default_factory=list)
    #: Deterministic statement of what the assessment does not establish. Its own field
    #: rather than a limitation, because the limitation list is truncated for length.
    scope_statement: Optional[MFINarrativeClaim] = None


class MFINarrativeQAFlag(BaseModel):
    """Stable deterministic, Red-Team, or system narrative flag."""

    flag_id: str
    source: Literal["deterministic", "red_team", "system"]
    code: str
    severity: Literal["high", "medium", "low"]
    artifact_type: Literal[
        "context",
        "dimension",
        "market",
        "executive_summary",
        "global",
    ]
    artifact_id: Optional[str] = None
    field_name: Optional[str] = None
    claim_id: Optional[str] = None
    message: str
    recommendation: str = ""
    metric_ids: List[str] = Field(default_factory=list)
    document_ids: List[str] = Field(default_factory=list)
    expected_value: Optional[str] = None
    actual_value: Optional[str] = None
    repairable: bool = True
    review_batch_id: Optional[str] = None
    review_batch_ids: List[str] = Field(default_factory=list)
    delivery_disposition: Optional[
        Literal[
            "blocking",
            "retained_unverified_figure_for_delivery",
            "retained_unverified_for_delivery",
            "advisory",
        ]
    ] = None


class MFIRedTeamFlagDraft(BaseModel):
    """Provider-constrained semantic flag before application identity assignment."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1)
    severity: Literal["high", "medium", "low"]
    artifact_type: Literal[
        "context",
        "dimension",
        "market",
        "executive_summary",
        "global",
    ]
    artifact_id: Optional[str] = None
    field_name: Optional[str] = None
    claim_id: Optional[str] = None
    message: str = Field(min_length=1)
    recommendation: str = ""
    metric_ids: List[str] = Field(default_factory=list)
    document_ids: List[str] = Field(default_factory=list)
    repairable: bool = True


class MFICorrectionTask(BaseModel):
    """One sequential live repair for exactly one canonical artifact field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1, le=3)
    artifact_type: Literal[
        "context", "dimension", "market", "executive_summary"
    ]
    artifact_id: str = Field(min_length=1)
    field_name: str = Field(min_length=1)
    claim_ids: List[str] = Field(default_factory=list)
    flag_ids: List[str] = Field(default_factory=list, min_length=1)
    flag_codes: List[str] = Field(default_factory=list, min_length=1)


class MFIFieldPatch(BaseModel):
    """Transport envelope for a field-specific correction response."""

    model_config = ConfigDict(extra="forbid")

    replacement: Any


class MFIClaimPatchValue(BaseModel):
    """Claim transport used only inside one field-level correction response."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    segments: List[NarrativeSegment] = Field(default_factory=list)
    fact_ids: List[str] = Field(default_factory=list)
    source_passages: List[SourcePassage] = Field(default_factory=list)
    claim_kind: Optional[Literal[
        "summary",
        "finding",
        "geographic_pattern",
        "limitation",
        "recommendation",
        "context",
    ]] = None
    metric_ids: List[str]
    document_ids: List[str]
    scope: Literal[
        "assessment", "region", "market", "surveyed_traders", "context"
    ]
    polarity: Literal["favorable", "unfavorable", "neutral", "descriptive"]


class MFIClaimFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: Optional[MFIClaimPatchValue]


class MFIClaimListFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: List[MFIClaimPatchValue]


class MFISubdimensionPatchValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    subsection_metric_id: Optional[str] = None
    score_0_10: Optional[float] = None
    interpretation: MFIClaimPatchValue
    driver_metric_ids: List[str] = Field(default_factory=list)


class MFISubdimensionFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: List[MFISubdimensionPatchValue]


class MFIContextTextFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: str = Field(min_length=1)


class MFIContextClassificationFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: Literal[
        "corroborating", "potentially_explanatory", "unrelated"
    ]


class MFIContextDocumentsFieldPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: List[str]


class MFIContextWithdrawalPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement: Literal[True]


class MFIRedTeamBatchDiagnostic(BaseModel):
    """Sanitized execution record for one deterministic Red-Team batch."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1)
    batch_kind: Literal["local", "dimension_coherence", "market_coherence"]
    contract_version: str = Field(default="mfi-red-team-batches-v1", min_length=1)
    shard_key: str = Field(default="legacy", min_length=1)
    sequence: int = Field(ge=1)
    artifact_refs: List[str] = Field(default_factory=list)
    scope_artifact_ref: Optional[str] = None
    character_count: int = Field(ge=0)
    target_character_count: int = Field(default=45_000, ge=1)
    claim_count: int = Field(ge=0)
    status: Literal["pending", "completed", "failed", "retained"]
    call_id: Optional[str] = None
    flag_count: int = Field(default=0, ge=0)
    failure_code: Optional[str] = None


class MFIRedTeamReviewBatch(BaseModel):
    """Immutable internal package for one bounded Red-Team invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(min_length=1)
    signature: str = Field(min_length=1)
    batch_kind: Literal["local", "dimension_coherence", "market_coherence"]
    shard_key: str = Field(default="legacy", min_length=1)
    sequence: int = Field(ge=1)
    package: Dict[str, Any]
    claim_ids: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    character_count: int = Field(ge=0)


class MFIRedTeamResponse(BaseModel):
    """Single JSON root required from Red-Team and format repair calls."""

    model_config = ConfigDict(extra="forbid")

    flags: List[MFIRedTeamFlagDraft] = Field(default_factory=list)


class MFIClaimValidationResult(BaseModel):
    """Deterministic validation result for all structured claims."""

    status: Literal[
        "not_recorded",
        "passed",
        "passed_with_warnings",
        "failed",
    ] = "not_recorded"
    validated_claim_count: int = 0
    verified_claim_count: int = 0
    unverified_claim_count: int = 0
    flags: List[MFINarrativeQAFlag] = Field(default_factory=list)


class MFICorrectionTarget(BaseModel):
    expected_field_hash: Optional[str] = None
    expected_field_revision: Optional[int] = None
    patch_kind: Optional[str] = None
    """Exact narrative field selected for a targeted repair."""

    artifact_type: Literal[
        "context",
        "dimension",
        "market",
        "executive_summary",
        "global",
    ]
    artifact_id: Optional[str] = None
    field_name: Optional[str] = None
    claim_ids: List[str] = Field(default_factory=list)
    flag_ids: List[str] = Field(default_factory=list)


class MFICorrectionAttemptRecord(BaseModel):
    """Audit record for one claim or artifact targeted in a correction cycle."""

    attempt_number: int = Field(ge=1)
    task_id: Optional[str] = None
    artifact_type: Literal[
        "context",
        "dimension",
        "market",
        "executive_summary",
        "global",
    ]
    artifact_id: Optional[str] = None
    field_name: Optional[str] = None
    claim_id: Optional[str] = None
    flag_ids: List[str] = Field(default_factory=list)
    flag_codes: List[str] = Field(default_factory=list)
    execution_outcome: Literal[
        "pending",
        "llm_completed",
        "deterministic_fallback",
        "llm_or_schema_failed",
        "not_executed",
    ] = "pending"
    validation_outcome: Literal[
        "pending",
        "resolved",
        "partially_resolved",
        "unresolved",
    ] = "pending"


class MFIQAReview(BaseModel):
    """Final combined deterministic and LLM QA status."""

    status: Literal[
        "not_recorded",
        "passed",
        "passed_with_advisories",
        "completed_with_warnings",
        "delivered_with_unverified_figures",
    ] = "not_recorded"
    correction_attempts: int = 0
    correction_history: List[MFICorrectionAttemptRecord] = Field(
        default_factory=list
    )
    flags: List[MFINarrativeQAFlag] = Field(default_factory=list)
    unverified_figure_flag_ids: List[str] = Field(default_factory=list)
    unverified_figure_claim_ids: List[str] = Field(default_factory=list)
    unverified_figure_values: List[str] = Field(default_factory=list)


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


class MFINarrativeArtifactDiagnostics(BaseModel):
    """Drafting provenance for one collection of narrative artifacts."""

    llm: List[str] = Field(default_factory=list)
    fallback: List[str] = Field(default_factory=list)


class MFIGenerationDiagnostics(BaseModel):
    """Operational diagnostics used to qualify Phase 4 pilot runs."""

    dimensions: MFINarrativeArtifactDiagnostics = Field(
        default_factory=MFINarrativeArtifactDiagnostics
    )
    markets: MFINarrativeArtifactDiagnostics = Field(
        default_factory=MFINarrativeArtifactDiagnostics
    )
    narrative_orchestration_version: str = "mfi-narrative-simple-v1"
    draft_batches_total: int = Field(default=0, ge=0)
    draft_batches_completed: int = Field(default=0, ge=0)
    draft_batches_failed: int = Field(default=0, ge=0)
    draft_batches: List[Dict[str, Any]] = Field(default_factory=list)
    market_prompt_projection_version: str = "mfi-market-prompt-v1"
    market_draft_prompt_max_characters: int = Field(default=160_000, gt=0)
    market_draft_max_observed_prompt_characters: int = Field(default=0, ge=0)
    market_draft_timeout_seconds: Optional[float] = Field(default=None, gt=0)
    semantic_reviews_total: int = Field(default=0, ge=0)
    semantic_reviews_completed: int = Field(default=0, ge=0)
    semantic_reviews_failed: int = Field(default=0, ge=0)
    semantic_reviews: List[Dict[str, Any]] = Field(default_factory=list)
    consolidated_correction_status: Literal[
        "not_needed", "pending", "completed", "failed"
    ] = "not_needed"
    consolidated_correction_call_id: Optional[str] = None
    consolidated_correction_field_count: int = Field(default=0, ge=0)
    consolidated_correction_llm_calls: int = Field(default=0, ge=0)
    consolidated_correction_prompt_character_count: int = Field(default=0, ge=0)
    consolidated_correction_prompt_max_characters: int = Field(
        default=600_000, gt=0
    )
    corrected_claim_verification_status: Literal[
        "not_needed", "pending", "completed", "failed"
    ] = "not_needed"
    corrected_claim_verification_call_id: Optional[str] = None
    corrected_claim_verification_package_character_count: int = Field(
        default=0, ge=0
    )
    corrected_claim_verification_prompt_character_count: int = Field(
        default=0, ge=0
    )
    corrected_claim_verification_max_characters: int = Field(
        default=400_000, gt=0
    )
    context_extraction_mode: Literal[
        "not_started",
        "llm",
        "fallback",
        "failed",
        "not_applicable",
    ] = "not_started"
    executive_summary_mode: Literal[
        "not_started",
        "llm",
        "fallback",
    ] = "not_started"
    red_team_status: Literal[
        "not_started",
        "in_progress",
        "completed",
        "failed",
    ] = "not_started"
    context_classification_status: Literal[
        "not_started", "not_attempted", "completed", "failed"
    ] = "not_started"
    red_team_contract_version: Optional[str] = None
    red_team_review_operation: Optional[str] = None
    red_team_structured_output: bool = False
    red_team_package_character_count: int = Field(default=0, ge=0)
    red_team_package_target_characters: int = Field(default=0, ge=0)
    red_team_package_within_target: Optional[bool] = None
    red_team_format_repair_attempted: bool = False
    red_team_format_repair_status: Literal[
        "not_needed",
        "completed",
        "failed",
    ] = "not_needed"
    red_team_initial_call_id: Optional[str] = None
    red_team_format_repair_call_id: Optional[str] = None
    correction_attempts: int = 0
    unresolved_high_count: int = 0
    unresolved_medium_count: int = 0
    unresolved_low_count: int = 0
    blocking_high_count: int = Field(default=0, ge=0)
    unverified_figure_flag_count: int = Field(default=0, ge=0)
    unverified_figure_claim_count: int = Field(default=0, ge=0)
    unverified_figure_flag_ids: List[str] = Field(default_factory=list)
    unverified_figure_values: List[str] = Field(default_factory=list)
    delivery_qa_status: Literal[
        "not_evaluated",
        "passed",
        "passed_with_advisories",
        "completed_with_warnings",
        "delivered_with_unverified_figures",
        "blocked",
    ] = "not_evaluated"
    retrievers: Dict[str, str] = Field(default_factory=dict)
    claim_substitutions: List[Dict[str, Any]] = Field(default_factory=list)
    unmatched_high_claim_ids: List[str] = Field(default_factory=list)
    claim_identity_authority: Literal["application"] = "application"
    claim_identity_version: Literal["mfi-claim-id-v1", "mfi-claim-id-v2"] = "mfi-claim-id-v2"
    ignored_model_identifier_count: int = Field(default=0, ge=0)
    ignored_correction_metadata_field_count: int = Field(default=0, ge=0)
    identity_fallback_artifacts: List[str] = Field(default_factory=list)
    delivery_contract_status: Literal[
        "not_validated",
        "validated",
        "fallback_validated",
        "failed",
    ] = "not_validated"
    fallback_policy: Literal["disabled_live", "offline_fixture"] = "disabled_live"
    correction_tasks_total: int = Field(default=0, ge=0)
    correction_tasks_completed: int = Field(default=0, ge=0)
    correction_tasks_failed: int = Field(default=0, ge=0)
    active_correction_task: Optional[str] = None
    red_team_batches_total: int = Field(default=0, ge=0)
    red_team_batches_completed: int = Field(default=0, ge=0)
    red_team_batches_failed: int = Field(default=0, ge=0)
    red_team_batches_retained: int = Field(default=0, ge=0)
    red_team_batches_pending: int = Field(default=0, ge=0)
    red_team_batches_by_kind: Dict[str, int] = Field(default_factory=dict)
    red_team_max_batch_character_count: int = Field(default=0, ge=0)
    active_red_team_batch: Optional[str] = None
    failed_red_team_batch: Optional[str] = None
    failed_red_team_batch_kind: Optional[str] = None
    failed_red_team_shard_key: Optional[str] = None
    failed_red_team_artifact: Optional[str] = None
    failed_red_team_character_count: Optional[int] = Field(default=None, ge=0)
    red_team_batches: List[MFIRedTeamBatchDiagnostic] = Field(default_factory=list)


class MFIMarketScoreDistributionEntry(BaseModel):
    """Neutral ordered market-score value for public consumers and charts."""

    market_name: str
    overall_mfi: float
    score_rank: int
    selection_order: int
    is_priority_market: bool


# ============================================================================
# PYDANTIC MODELS (API)
# ============================================================================

class GenerateMFIReportInput(BaseModel):
    """Input for generating MFI report."""
    country: str = Field(..., description="Country name")
    data_collection_start: str = Field(..., description="Data collection start date (YYYY-MM-DD)")
    data_collection_end: str = Field(..., description="Data collection end date (YYYY-MM-DD)")
    markets: List[str] = Field(..., description="List of surveyed markets")
    use_mock_data: bool = Field(False, description="Required: this JSON input carries no survey data, "
                                "so the report is built on synthetic demonstration data")


class GenerateMFIReportFromCSVInput(BaseModel):
    """Input for generating MFI report from uploaded CSV."""
    country_override: Optional[str] = Field(None, description="Override country name from CSV")
    data_collection_start_override: Optional[str] = Field(None, description="Override start date")
    data_collection_end_override: Optional[str] = Field(None, description="Override end date")


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


class GenerateMFIReportOutput(BaseModel):
    workflow_revision: Optional[str] = None
    """Output of MFI report generation."""
    run_id: str
    country: str
    data_collection_start: str
    data_collection_end: str

    analysis_schema_version: Literal["2.0", "2.1"] = ANALYSIS_SCHEMA_VERSION
    methodology_version: Literal["databridge-current"] = METHODOLOGY_VERSION
    score_authority: Literal["databridge_level_1", "synthetic_mock"] = SCORE_AUTHORITY
    narrative_schema_version: Literal["2.0", "2.1"] = NARRATIVE_SCHEMA_VERSION
    release_control: MFIReleaseControl
    generation_diagnostics: MFIGenerationDiagnostics = Field(
        default_factory=MFIGenerationDiagnostics
    )
    llm_diagnostics: LLMRunDiagnostics = Field(
        default_factory=lambda: LLMRunDiagnostics(
            service="mfi-drafter",
            run_id="unknown",
        )
    )
    excluded_market_records: List[MFIExcludedMarketRecord] = Field(default_factory=list)
    methodology_warnings: List[MFIMethodologyWarning] = Field(default_factory=list)
    
    # Survey info
    survey_metadata: Dict[str, Any]
    
    # MFI Data
    national_mfi: float = Field(
        ...,
        deprecated=True,
        description="Deprecated output-only alias; use mean_mfi_across_assessed_markets.",
    )
    risk_distribution: Dict[str, int] = Field(
        ...,
        deprecated=True,
        description="Deprecated output-only alias; use market_score_distribution.",
    )
    markets_data: List[Dict[str, Any]]
    dimension_scores: List[Dict[str, Any]] = Field(
        ...,
        deprecated=True,
        description="Deprecated output-only aggregation alias.",
    )
    mean_mfi_across_assessed_markets: float
    assessment_profile: MFIAssessmentProfile
    market_score_distribution: List[MFIMarketScoreDistributionEntry] = Field(
        default_factory=list
    )

    # Canonical structured narratives and verification.
    context_status: MFIContextStatus = Field(
        default_factory=lambda: MFIContextStatus(
            status="not_attempted",
            retrievers={},
            total_deduplicated_documents_retrieved=0,
            statements_classified=0,
            final_accepted_statements=0,
            extraction_mode="offline",
        )
    )
    context_evidence: List[MFIContextEvidenceStatement] = Field(default_factory=list)
    dimension_narratives: Dict[str, MFIDimensionNarrative] = Field(
        default_factory=dict
    )
    market_narratives: Dict[str, MFIMarketNarrative] = Field(default_factory=dict)
    executive_summary_narrative: MFIExecutiveNarrative = Field(
        default_factory=MFIExecutiveNarrative
    )
    claim_validation: MFIClaimValidationResult = Field(
        default_factory=MFIClaimValidationResult
    )
    qa_review: MFIQAReview = Field(default_factory=MFIQAReview)
    
    # Generated content
    executive_summary: str
    dimension_findings: Dict[str, Dict[str, str]]
    market_recommendations: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    country_context: Optional[str] = None

    document_references: List[Dict[str, Any]] = Field(default_factory=list)

    report_blocks: List[ReportBlock] = Field(default_factory=list)
    
    # Visualizations (Base64)
    visualizations: Dict[str, str]
    
    # Control
    warnings: List[str] = Field(default_factory=list)
    llm_calls: int = 0
    correction_attempts: int = 0
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
