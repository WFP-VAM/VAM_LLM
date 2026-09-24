"""Authoritative current-DataBridge methodology for the Full MFI drafter.

The registry in this module is deliberately keyed by the exact processed-data
identity ``(LevelID, DimensionName, VariableName)``.  Callers may trim the
fixed-width strings emitted by DataBridge, but must not perform substring,
prefix, or case-insensitive matching.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from math import sqrt
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, Optional

METHODOLOGY_VERSION = "databridge-current"
ANALYSIS_SCHEMA_VERSION = "2.1"
SCORE_AUTHORITY = "databridge_level_1"
SCORE_VALIDATION_ABS_TOLERANCE = 1e-6
CURRENT_DATABRIDGE_ALPHA = 0.5
CURRENT_DATABRIDGE_BETA = 0.5

MetricRole = Literal[
    "official_score",
    "official_subsection",
    "dimension_validation_component",
    "question_driver",
    "category_driver",
    "item_driver",
]
MetricOrientation = Literal["higher_is_better", "higher_is_worse", "descriptive"]
MetricUnit = Literal["score", "proportion"]
EvidenceScope = Literal["assessed_market", "surveyed_traders_in_market"]
ApplicabilityRule = Literal[
    "required",
    "optional_product_group",
    "optional_item",
    "quality_applicability",
]
NormalizationKind = Literal[
    "identity_0_10",
    "linear_zero_max",
    "assortment_breadth",
    "assortment_depth",
    "quality_ratio",
    "none",
]

LEVEL_NAMES: Mapping[int, str] = MappingProxyType(
    {
        1: "Normalized Score",
        2: "Trader Aggregate Score",
        3: "Market Aggregate Score",
        4: "Trader Median",
        5: "Trader Mean",
        6: "Market Mean",
    }
)

CSV_DIMENSION_TO_DISPLAY: Mapping[str, str] = MappingProxyType(
    {
        "Assortment": "Assortment",
        "Availability": "Availability",
        "Price": "Price",
        "Resilience": "Resilience",
        "Competition": "Competition",
        "Infrastructure": "Infrastructure",
        "Service": "Service",
        "Quality": "Food Quality",
        "AccessProtection": "Access & Protection",
        "MFI": "MFI",
    }
)

DISPLAY_DIMENSIONS = (
    "Assortment",
    "Availability",
    "Price",
    "Resilience",
    "Competition",
    "Infrastructure",
    "Service",
    "Food Quality",
    "Access & Protection",
)

DIMENSION_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "Assortment": (
            "Assortment measures whether assessed markets carry the eight "
            "essential-goods groups and the depth of choice within those groups, "
            "represented by the maximum SKU-depth category."
        ),
        "Availability": (
            "Availability measures whether essential food and non-food categories "
            "are generally represented and whether traders report scarcity or "
            "running out of stock."
        ),
        "Price": (
            "Price measures recent price increases and short-term price "
            "predictability. It does not by itself measure affordability, "
            "inflation, or household purchasing power."
        ),
        "Resilience": (
            "Resilience measures stock duration, replenishment lead time, supplier "
            "geographic concentration, supplier diversity, and dependence on a "
            "single supplier."
        ),
        "Competition": (
            "Competition measures the presence of competing traders and the "
            "absence of a single trader controlling supply in the assessed market."
        ),
        "Infrastructure": (
            "Infrastructure measures market-site condition and the presence of "
            "facilities such as storage, sanitation, electricity, water, waste "
            "management, and access infrastructure."
        ),
        "Service": (
            "Service measures observable retail practices: product display, "
            "visible prices, remote purchasing, accepted payment methods, waiting "
            "time, and automatic receipts."
        ),
        "Food Quality": (
            "Food Quality measures the share of applicable food-handling and "
            "protection conditions satisfied in each assessed market. Applicability "
            "and the dynamic maximum must remain explicit."
        ),
        "Access & Protection": (
            "Access & Protection measures road remoteness, seasonal and "
            "disaster-related access, social barriers, physical threats, and "
            "general security affecting access to assessed markets."
        ),
    }
)


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    dimension: str
    csv_dimension: str
    variable_name: str
    source_level_id: int
    source_level_name: str
    role: MetricRole
    display_name: str
    raw_min: float
    raw_max: float
    normalization: NormalizationKind
    orientation: MetricOrientation
    unit: MetricUnit
    evidence_scope: EvidenceScope
    applicability_rule: ApplicabilityRule
    methodology_note: str
    product_group: Optional[str] = None
    question_group: Optional[str] = None
    item_name: Optional[str] = None
    severity_weight: Optional[int] = None
    parent_subsection_id: Optional[str] = None

    @property
    def key(self) -> tuple[int, str, str]:
        return (self.source_level_id, self.csv_dimension, self.variable_name)

    def normalize(self, raw_value: float, *, dynamic_max: Optional[float] = None) -> Optional[float]:
        """Return an independent 0-10 display value when one is defined."""
        value = float(raw_value)
        if self.normalization == "identity_0_10":
            return value
        if self.normalization == "linear_zero_max":
            if self.raw_max <= 0:
                return None
            return value / self.raw_max * 10.0
        if self.normalization == "assortment_breadth":
            return (value - 1.0) / 7.0 * 10.0
        if self.normalization == "assortment_depth":
            return (value - 1.0) / 3.0 * 10.0
        if self.normalization == "quality_ratio":
            if dynamic_max is None or dynamic_max <= 0:
                return None
            return value / float(dynamic_max) * 10.0
        return None


def _definition(
    *,
    metric_id: str,
    csv_dimension: str,
    variable_name: str,
    level: int,
    role: MetricRole,
    display_name: str,
    raw_min: float,
    raw_max: float,
    normalization: NormalizationKind,
    orientation: MetricOrientation = "higher_is_better",
    unit: MetricUnit = "score",
    scope: EvidenceScope = "assessed_market",
    applicability: ApplicabilityRule = "required",
    note: str,
    product_group: Optional[str] = None,
    question_group: Optional[str] = None,
    item_name: Optional[str] = None,
    severity_weight: Optional[int] = None,
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        dimension=CSV_DIMENSION_TO_DISPLAY[csv_dimension],
        csv_dimension=csv_dimension,
        variable_name=variable_name,
        source_level_id=level,
        source_level_name=LEVEL_NAMES[level],
        role=role,
        display_name=display_name,
        raw_min=raw_min,
        raw_max=raw_max,
        normalization=normalization,
        orientation=orientation,
        unit=unit,
        evidence_scope=scope,
        applicability_rule=applicability,
        methodology_note=note,
        product_group=product_group,
        question_group=question_group,
        item_name=item_name,
        severity_weight=severity_weight,
    )


def _official_score_definitions() -> list[MetricDefinition]:
    entries = [
        ("assortment.dimension.score", "Assortment", "AssortmentScoreMFI", "Assortment score"),
        ("availability.dimension.score", "Availability", "AvailabilityScoreMFI", "Availability score"),
        ("price.dimension.score", "Price", "PriceScoreMFI", "Price score"),
        ("resilience.dimension.score", "Resilience", "ResilienceScoreMFI", "Resilience score"),
        ("competition.dimension.score", "Competition", "CompetitionScoreMFI", "Competition score"),
        (
            "infrastructure.dimension.score",
            "Infrastructure",
            "InfrastructureScoreMFI",
            "Infrastructure score",
        ),
        ("service.dimension.score", "Service", "ServiceScoreMFI", "Service score"),
        ("quality.dimension.score", "Quality", "QualityScoreMFI", "Food Quality score"),
        (
            "access_protection.dimension.score",
            "AccessProtection",
            "AccessProtectionScoreMFI",
            "Access & Protection score",
        ),
        ("mfi.overall", "MFI", "MFIScoreMFI", "Overall MFI"),
    ]
    return [
        _definition(
            metric_id=metric_id,
            csv_dimension=dimension,
            variable_name=variable_name,
            level=1,
            role="official_score",
            display_name=display_name,
            raw_min=0,
            raw_max=10,
            normalization="identity_0_10",
            note="Authoritative stored DataBridge Level-1 score; never replace with a local calculation.",
        )
        for metric_id, dimension, variable_name, display_name in entries
    ]


def _subsection_definitions() -> list[MetricDefinition]:
    return [
        _definition(
            metric_id="assortment.breadth",
            csv_dimension="Assortment",
            variable_name="AssortmentBreadth",
            level=6,
            role="official_subsection",
            display_name="Assortment breadth",
            raw_min=1,
            raw_max=8,
            normalization="assortment_breadth",
            note="Official breadth subsection; independent display normalization only.",
            question_group="breadth",
        ),
        _definition(
            metric_id="assortment.depth",
            csv_dimension="Assortment",
            variable_name="AssortmentDepth",
            level=6,
            role="official_subsection",
            display_name="Assortment depth",
            raw_min=1,
            raw_max=4,
            normalization="assortment_depth",
            note="Official depth subsection; independent display normalization only.",
            question_group="depth",
        ),
        _definition(
            metric_id="availability.scarcity",
            csv_dimension="Availability",
            variable_name="AvailabilityScarcity",
            level=4,
            role="official_subsection",
            display_name="Absence of scarcity",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable scarcity subsection after DataBridge polarity adjustment.",
            question_group="scarcity",
        ),
        _definition(
            metric_id="availability.runout",
            csv_dimension="Availability",
            variable_name="AvailabilityRunout",
            level=4,
            role="official_subsection",
            display_name="Absence of imminent runout",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable runout subsection after DataBridge polarity adjustment.",
            question_group="runout",
        ),
        _definition(
            metric_id="price.increase",
            csv_dimension="Price",
            variable_name="PriceIncrease",
            level=4,
            role="official_subsection",
            display_name="Absence of recent large price increases",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable price-trend subsection after DataBridge polarity adjustment.",
            question_group="increase",
        ),
        _definition(
            metric_id="price.stability",
            csv_dimension="Price",
            variable_name="PriceStability",
            level=4,
            role="official_subsection",
            display_name="Price stability and predictability",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable price-stability subsection.",
            question_group="stability",
        ),
        _definition(
            metric_id="resilience.responsiveness",
            csv_dimension="Resilience",
            variable_name="ResilienceResponsive",
            level=4,
            role="official_subsection",
            display_name="Supply-chain responsiveness",
            raw_min=0,
            raw_max=2,
            normalization="linear_zero_max",
            note="Official responsiveness subsection.",
            question_group="responsiveness",
        ),
        _definition(
            metric_id="resilience.vulnerability",
            csv_dimension="Resilience",
            variable_name="ResilienceVulnerability",
            level=4,
            role="official_subsection",
            display_name="Low vulnerability to disruption",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable vulnerability subsection after polarity adjustment.",
            question_group="vulnerability",
        ),
        _definition(
            metric_id="competition.concentration",
            csv_dimension="Competition",
            variable_name="CompetitionConcentration",
            level=6,
            role="official_subsection",
            display_name="Sufficient trader competition",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable concentration subsection after polarity adjustment.",
            question_group="concentration",
        ),
        _definition(
            metric_id="competition.monopoly",
            csv_dimension="Competition",
            variable_name="CompetitionMonopoly",
            level=6,
            role="official_subsection",
            display_name="Absence of single-trader control",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable monopoly subsection after polarity adjustment.",
            question_group="monopoly",
        ),
        _definition(
            metric_id="infrastructure.condition",
            csv_dimension="Infrastructure",
            variable_name="InfrastructureCondition",
            level=6,
            role="official_subsection",
            display_name="Infrastructure condition",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official infrastructure-condition subsection.",
            question_group="condition",
        ),
        _definition(
            metric_id="infrastructure.features",
            csv_dimension="Infrastructure",
            variable_name="InfrastructureFeatures",
            level=6,
            role="official_subsection",
            display_name="Infrastructure features",
            raw_min=0,
            raw_max=8,
            normalization="linear_zero_max",
            note="Official infrastructure-features subsection.",
            question_group="features",
        ),
        _definition(
            metric_id="service.shopping",
            csv_dimension="Service",
            variable_name="ServiceShopping",
            level=4,
            role="official_subsection",
            display_name="Shopping service",
            raw_min=0,
            raw_max=3,
            normalization="linear_zero_max",
            note="Official shopping-service subsection.",
            question_group="shopping",
        ),
        _definition(
            metric_id="service.checkout",
            csv_dimension="Service",
            variable_name="ServiceCheckout",
            level=4,
            role="official_subsection",
            display_name="Checkout service",
            raw_min=0,
            raw_max=3,
            normalization="linear_zero_max",
            note="Official checkout-service subsection.",
            question_group="checkout",
        ),
        _definition(
            metric_id="quality.measure",
            csv_dimension="Quality",
            variable_name="QualityCMeasureMFI",
            level=3,
            role="dimension_validation_component",
            display_name="Applicable quality conditions satisfied",
            raw_min=0,
            raw_max=8,
            normalization="quality_ratio",
            note="Validation numerator; Food Quality has question evidence rather than formal subsections.",
            question_group="quality_validation",
        ),
        _definition(
            metric_id="quality.maximum",
            csv_dimension="Quality",
            variable_name="QualityMaximum",
            level=3,
            role="dimension_validation_component",
            display_name="Applicable quality conditions",
            raw_min=0,
            raw_max=8,
            normalization="none",
            orientation="descriptive",
            note="Validation denominator reflecting question-specific applicability.",
            question_group="quality_validation",
        ),
        _definition(
            metric_id="access_protection.access",
            csv_dimension="AccessProtection",
            variable_name="AccessProtectionAccess",
            level=6,
            role="official_subsection",
            display_name="Physical access",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable access subsection after polarity adjustment.",
            question_group="access",
        ),
        _definition(
            metric_id="access_protection.protection",
            csv_dimension="AccessProtection",
            variable_name="AccessProtectionProtection",
            level=6,
            role="official_subsection",
            display_name="Protection and security",
            raw_min=0,
            raw_max=6,
            normalization="linear_zero_max",
            note="Official favorable protection subsection after polarity adjustment.",
            question_group="protection",
        ),
    ]


_CEREAL_ITEMS = (
    ("Barley", "barley", "Barley"),
    ("Bread", "bread", "Bread"),
    ("Cassava", "cassava", "Cassava"),
    ("Flour", "flour", "Flour"),
    ("Maize", "maize", "Maize"),
    ("Mill", "millet", "Millet"),
    ("Oth", "other", "Other cereal"),
    ("Pasta", "pasta", "Pasta"),
    ("Rice", "rice", "Rice"),
    ("Sorgh", "sorghum", "Sorghum"),
    ("Wheat", "wheat", "Wheat"),
)
_AVAILABILITY_PRICE_CEREAL_ITEMS = tuple(item for item in _CEREAL_ITEMS if item[0] != "Cassava")
_OTHER_FOOD_ITEMS = (
    ("Dairy", "dairy", "Milk and dairy products"),
    ("Fat", "oils_fats", "Oils and fats"),
    ("FruitVeg", "fruit_vegetables", "Fruits and vegetables"),
    ("Misc", "condiments_spices", "Herbs, condiments and spices"),
    ("Oth", "other", "Other non-cereal food"),
    ("PrMeatFishEgg", "meat_fish_eggs", "Meat, fish and eggs"),
    ("Pulse", "legumes_nuts_seeds", "Legumes, nuts and seeds"),
    ("Roo", "roots_tubers", "Roots and tubers"),
)
_NFI_ITEMS = (
    ("Assets", "household_items", "Household items"),
    ("Comm", "communication", "Communication"),
    ("Educ", "education", "Education"),
    ("Health", "health", "Health"),
    ("Shelter", "shelter", "Shelter"),
    ("Wash", "wash", "WASH"),
)


def _rate_driver(
    *,
    metric_id: str,
    csv_dimension: str,
    variable_name: str,
    display_name: str,
    role: MetricRole,
    orientation: MetricOrientation,
    scope: EvidenceScope,
    applicability: ApplicabilityRule,
    question_group: str,
    product_group: Optional[str] = None,
    item_name: Optional[str] = None,
    level: int = 5,
    note: str,
    severity_weight: Optional[int] = None,
) -> MetricDefinition:
    return _definition(
        metric_id=metric_id,
        csv_dimension=csv_dimension,
        variable_name=variable_name,
        level=level,
        role=role,
        display_name=display_name,
        raw_min=0,
        raw_max=1,
        normalization="linear_zero_max",
        orientation=orientation,
        unit="proportion",
        scope=scope,
        applicability=applicability,
        note=note,
        product_group=product_group,
        question_group=question_group,
        item_name=item_name,
        severity_weight=severity_weight,
    )


def _assortment_drivers() -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = []
    broad = (
        ("FCer", "cereal_food", "Cereal food"),
        ("FOth", "other_food", "Other food"),
        ("NFAssets", "household_items", "NFI - household items"),
        ("NFComm", "communication", "NFI - communication"),
        ("NFEduc", "education", "NFI - education"),
        ("NFHealth", "health", "NFI - health"),
        ("NFShelter", "shelter", "NFI - shelter"),
        ("NFWash", "wash", "NFI - WASH"),
    )
    for suffix, slug, label in broad:
        definitions.append(
            _rate_driver(
                metric_id=f"assortment.category.{slug}",
                csv_dimension="Assortment",
                variable_name=f"Assortment_{suffix}",
                display_name=label,
                role="category_driver",
                orientation="higher_is_better",
                scope="surveyed_traders_in_market",
                applicability="required",
                question_group="breadth",
                product_group=slug,
                note="Positive assortment evidence among surveyed traders.",
            )
        )
    sku_labels = (
        ("1", "1_50", "SKU depth: 1-50"),
        ("2", "51_200", "SKU depth: 51-200"),
        ("3", "201_1000", "SKU depth: 201-1,000"),
        ("4", "more_than_1000", "SKU depth: more than 1,000"),
    )
    for class_number, slug, label in sku_labels:
        definitions.append(
            _rate_driver(
                metric_id=f"assortment.sku_class.{slug}",
                csv_dimension="Assortment",
                variable_name=f"Assortment_SKUClass{class_number}",
                display_name=label,
                role="question_driver",
                orientation="descriptive",
                scope="surveyed_traders_in_market",
                applicability="required",
                question_group="depth",
                note="Mutually exclusive descriptive SKU-depth class.",
            )
        )
    for prefix, group_slug, group_label, items in (
        ("FCer", "cereal_food", "Cereal", _CEREAL_ITEMS),
        ("FOth", "other_food", "Other food", _OTHER_FOOD_ITEMS),
    ):
        for suffix, item_slug, item_label in items:
            definitions.append(
                _rate_driver(
                    metric_id=f"assortment.item.{group_slug}.{item_slug}",
                    csv_dimension="Assortment",
                    variable_name=f"Assortment_{prefix}{suffix}",
                    display_name=f"{group_label} assortment - {item_label}",
                    role="item_driver",
                    orientation="higher_is_better",
                    scope="surveyed_traders_in_market",
                    applicability="optional_item",
                    question_group="breadth",
                    product_group=group_slug,
                    item_name=item_label,
                    note="Positive item-presence evidence among surveyed traders.",
                )
            )
    return definitions


def _availability_drivers() -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = []
    for question, label in (("Scarcity", "Not scarce"), ("Runout", "Not running out")):
        for suffix, group_slug, group_label in (
            ("FCer", "cereal_food", "Cereal food"),
            ("FOth", "other_food", "Other food"),
            ("NF", "nfi", "Non-food items"),
        ):
            definitions.append(
                _rate_driver(
                    metric_id=f"availability.{question.lower()}.category.{group_slug}",
                    csv_dimension="Availability",
                    variable_name=f"Availability{question}_{suffix}",
                    display_name=f"{label} - {group_label}",
                    role="category_driver",
                    orientation="higher_is_better",
                    scope="surveyed_traders_in_market",
                    applicability="required",
                    question_group=question.lower(),
                    product_group=group_slug,
                    note="Favorable category rate from the processed Trader Mean row.",
                )
            )
        for prefix, group_slug, group_label, items in (
            ("FCer", "cereal_food", "Cereal", _AVAILABILITY_PRICE_CEREAL_ITEMS),
            ("FOth", "other_food", "Other food", _OTHER_FOOD_ITEMS),
            ("NF", "nfi", "NFI", _NFI_ITEMS),
        ):
            for suffix, item_slug, item_label in items:
                definitions.append(
                    _rate_driver(
                        metric_id=f"availability.{question.lower()}.item.{group_slug}.{item_slug}",
                        csv_dimension="Availability",
                        variable_name=f"Availability{question}_{prefix}{suffix}",
                        display_name=f"{question} problem rate - {group_label}: {item_label}",
                        role="item_driver",
                        orientation="higher_is_worse",
                        scope="surveyed_traders_in_market",
                        applicability="optional_item",
                        question_group=question.lower(),
                        product_group=group_slug,
                        item_name=item_label,
                        note="Item row already expresses a problem rate and must not be inverted.",
                    )
                )
    return definitions


def _price_drivers() -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = []
    for variable_prefix, question_slug, category_label in (
        ("PriceIncrease", "increase", "Price not increased"),
        ("PriceStability", "stability", "Price stability"),
    ):
        for suffix, group_slug, group_label in (
            ("FCer", "cereal_food", "Cereal food"),
            ("FOth", "other_food", "Other food"),
            ("NF", "nfi", "Non-food items"),
        ):
            definitions.append(
                _rate_driver(
                    metric_id=f"price.{question_slug}.category.{group_slug}",
                    csv_dimension="Price",
                    variable_name=f"{variable_prefix}_{suffix}",
                    display_name=f"{category_label} - {group_label}",
                    role="category_driver",
                    orientation="higher_is_better",
                    scope="surveyed_traders_in_market",
                    applicability="required",
                    question_group=question_slug,
                    product_group=group_slug,
                    note="Favorable category rate from the processed Trader Mean row.",
                )
            )
    for variable_prefix, question_slug, problem_label in (
        ("PriceIncrease", "increase", "Price increase"),
        ("PriceUnstable", "stability", "Price instability"),
    ):
        for prefix, group_slug, group_label, items in (
            ("FCer", "cereal_food", "Cereal", _AVAILABILITY_PRICE_CEREAL_ITEMS),
            ("FOth", "other_food", "Other food", _OTHER_FOOD_ITEMS),
            ("NF", "nfi", "NFI", _NFI_ITEMS),
        ):
            for suffix, item_slug, item_label in items:
                definitions.append(
                    _rate_driver(
                        metric_id=f"price.{question_slug}.item.{group_slug}.{item_slug}",
                        csv_dimension="Price",
                        variable_name=f"{variable_prefix}_{prefix}{suffix}",
                        display_name=f"{problem_label} rate - {group_label}: {item_label}",
                        role="item_driver",
                        orientation="higher_is_worse",
                        scope="surveyed_traders_in_market",
                        applicability="optional_item",
                        question_group=question_slug,
                        product_group=group_slug,
                        item_name=item_label,
                        note="Item row already expresses a problem rate and must not be inverted.",
                    )
                )
    return definitions


def _resilience_drivers() -> list[MetricDefinition]:
    definitions = [
        _rate_driver(
            metric_id="resilience.responsiveness.current_stock",
            csv_dimension="Resilience",
            variable_name="ResilienceResponsiveCurrentstock",
            display_name="Stock expected to last at least one week",
            role="question_driver",
            orientation="higher_is_better",
            scope="surveyed_traders_in_market",
            applicability="required",
            question_group="responsiveness",
            note="Favorable processed response rate.",
        ),
        _rate_driver(
            metric_id="resilience.responsiveness.lead_time",
            csv_dimension="Resilience",
            variable_name="ResilienceResponsiveLeadtime",
            display_name="Replenishment expected within one week",
            role="question_driver",
            orientation="higher_is_better",
            scope="surveyed_traders_in_market",
            applicability="required",
            question_group="responsiveness",
            note="Favorable processed response rate.",
        ),
    ]
    for component, component_label in (
        ("Density", "Geographic supplier dispersion"),
        ("Complexity", "Supplier diversity"),
        ("Criticality", "Absence of single-supplier dependence"),
    ):
        for suffix, group_slug, group_label in (
            ("FCer", "cereal_food", "Cereal food"),
            ("FOth", "other_food", "Other food"),
            ("NF", "nfi", "Non-food items"),
        ):
            definitions.append(
                _rate_driver(
                    metric_id=f"resilience.vulnerability.{component.lower()}.{group_slug}",
                    csv_dimension="Resilience",
                    variable_name=f"ResilienceVulnerability{component}_{suffix}",
                    display_name=f"{component_label} - {group_label}",
                    role="category_driver",
                    orientation="higher_is_better",
                    scope="surveyed_traders_in_market",
                    applicability="required",
                    question_group="vulnerability",
                    product_group=group_slug,
                    note="Favorable processed component after question polarity adjustment.",
                )
            )
    return definitions


def _competition_drivers() -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = []
    for component, slug, label in (
        ("Less", "concentration", "At least five traders"),
        ("One", "monopoly", "No single trader controls the market"),
    ):
        for suffix, group_slug, group_label in (
            ("FCer", "cereal_food", "Cereal food"),
            ("FOth", "other_food", "Other food"),
            ("NF", "nfi", "Non-food items"),
        ):
            definitions.append(
                _rate_driver(
                    metric_id=f"competition.{slug}.{group_slug}",
                    csv_dimension="Competition",
                    variable_name=f"Competition{component}_{suffix}",
                    display_name=f"{label} - {group_label}",
                    role="category_driver",
                    orientation="higher_is_better",
                    scope="assessed_market",
                    applicability="optional_product_group",
                    question_group=slug,
                    product_group=group_slug,
                    level=6,
                    note="Favorable processed component after inversion of the negative question.",
                )
            )
    return definitions


def _infrastructure_drivers() -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = []
    for suffix, slug, label, orientation in (
        ("Good", "good", "Good condition", "higher_is_better"),
        ("Medium", "medium", "Medium condition", "descriptive"),
        ("Poor", "poor", "Poor condition", "higher_is_worse"),
    ):
        definitions.append(
            _rate_driver(
                metric_id=f"infrastructure.condition.{slug}",
                csv_dimension="Infrastructure",
                variable_name=f"InfrastructureCondition{suffix}",
                display_name=label,
                role="question_driver",
                orientation=orientation,
                scope="assessed_market",
                applicability="required",
                question_group="condition",
                level=6,
                note="Condition category; do not collapse categories to a threshold flag.",
            )
        )
    features = (
        ("Closedsewage", "closed_sewage", "Closed sewage system"),
        ("Electricity", "electricity", "Uninterrupted electricity"),
        ("Network", "communication_network", "Reliable communication network"),
        ("Nowaste", "waste_collection", "Dedicated waste collection"),
        ("Shelter", "shelter", "Shelter"),
        ("Toilet", "toilets", "Toilets"),
        ("Walkways", "walkways_exits", "Walkways and emergency exits"),
        ("Water", "water", "Water availability"),
    )
    for suffix, slug, label in features:
        definitions.append(
            _rate_driver(
                metric_id=f"infrastructure.feature.{slug}",
                csv_dimension="Infrastructure",
                variable_name=f"InfrastructureFeatures{suffix}",
                display_name=label,
                role="question_driver",
                orientation="higher_is_better",
                scope="assessed_market",
                applicability="required",
                question_group="features",
                level=6,
                note="Positive infrastructure-feature evidence.",
            )
        )
    return definitions


def _service_drivers() -> list[MetricDefinition]:
    definitions: list[MetricDefinition] = []
    for group, entries in (
        (
            "shopping",
            (
                ("ServiceShoppingDisplay", "display", "Products displayed for easy selection"),
                ("ServiceShoppingPrice", "visible_prices", "Visible price tags"),
                ("ServiceShoppingRemote", "remote_purchase", "Remote purchase available"),
            ),
        ),
        (
            "checkout",
            (
                ("ServiceCheckoutMultipayment", "multiple_payments", "Multiple forms of payment"),
                ("ServiceCheckoutWait", "short_wait", "Checkout wait below ten minutes"),
                ("ServiceCheckoutReceipt", "automatic_receipt", "Automatic itemized receipt"),
            ),
        ),
    ):
        for variable_name, slug, label in entries:
            definitions.append(
                _rate_driver(
                    metric_id=f"service.{group}.{slug}",
                    csv_dimension="Service",
                    variable_name=variable_name,
                    display_name=label,
                    role="question_driver",
                    orientation="higher_is_better",
                    scope="surveyed_traders_in_market",
                    applicability="required",
                    question_group=group,
                    note="Positive service condition among surveyed shops.",
                )
            )
    return definitions


def _quality_drivers() -> list[MetricDefinition]:
    entries = (
        ("QualityFood", "food_protection", "Food protected from contaminants", "required"),
        ("QualitySeparate", "separation", "Fresh food separated from animal-origin food", "quality_applicability"),
        ("QualityRefrigerate", "refrigeration", "Applicable food refrigerated", "quality_applicability"),
        (
            "QualityRefrigerateWork",
            "continuous_refrigeration",
            "Refrigeration continuously working",
            "quality_applicability",
        ),
        ("QualityExpiry", "expiry", "Products not expired", "required"),
        (
            "QualityPrepackaged",
            "prepackaged",
            "Prepackaged food intact and labelled",
            "quality_applicability",
        ),
        ("QualityNospoilage", "no_spoilage", "No visible spoilage", "required"),
        ("QualityPlastic", "packaging", "Packaging intact", "quality_applicability"),
    )
    return [
        _rate_driver(
            metric_id=f"quality.condition.{slug}",
            csv_dimension="Quality",
            variable_name=variable_name,
            display_name=label,
            role="question_driver",
            orientation="higher_is_better",
            scope="assessed_market",
            applicability=applicability,  # type: ignore[arg-type]
            question_group="quality",
            level=6,
            note="Positive quality condition with question-specific applicability.",
        )
        for variable_name, slug, label, applicability in entries
    ]


def _access_protection_drivers() -> list[MetricDefinition]:
    entries = (
        (
            "AccessProtectionAccessRoad",
            "access",
            "road",
            "Not far from a major road network",
            1,
        ),
        (
            "AccessProtectionAccessSeasonal",
            "access",
            "seasonal",
            "No seasonal access difficulty",
            2,
        ),
        (
            "AccessProtectionAccessDisaster",
            "access",
            "disaster",
            "No disaster-related access limitation",
            3,
        ),
        (
            "AccessProtectionProtectionSocial",
            "protection",
            "social",
            "No social barriers",
            1,
        ),
        (
            "AccessProtectionProtectionPhysical",
            "protection",
            "physical",
            "No physical threats",
            2,
        ),
        (
            "AccessProtectionProtectionSecurity",
            "protection",
            "security",
            "No general security issue",
            3,
        ),
    )
    return [
        _rate_driver(
            metric_id=f"access_protection.{group}.{slug}",
            csv_dimension="AccessProtection",
            variable_name=variable_name,
            display_name=label,
            role="question_driver",
            orientation="higher_is_better",
            scope="assessed_market",
            applicability="required",
            question_group=group,
            level=6,
            note="Favorable processed component; severity weight applies to the underlying issue.",
            severity_weight=severity,
        )
        for variable_name, group, slug, label, severity in entries
    ]


def _driver_definitions() -> list[MetricDefinition]:
    return [
        *_assortment_drivers(),
        *_availability_drivers(),
        *_price_drivers(),
        *_resilience_drivers(),
        *_competition_drivers(),
        *_infrastructure_drivers(),
        *_service_drivers(),
        *_quality_drivers(),
        *_access_protection_drivers(),
    ]


OFFICIAL_SCORE_DEFINITIONS = tuple(_official_score_definitions())
SUBSECTION_DEFINITIONS = tuple(_subsection_definitions())
DRIVER_DEFINITIONS = tuple(_driver_definitions())
_parents = {(d.dimension, d.question_group): d.metric_id for d in SUBSECTION_DEFINITIONS}
DRIVER_DEFINITIONS = tuple(replace(d, parent_subsection_id="quality.measure" if d.dimension == "Food Quality" else _parents.get((d.dimension, d.question_group))) for d in DRIVER_DEFINITIONS)
if any(d.parent_subsection_id not in {s.metric_id for s in SUBSECTION_DEFINITIONS} for d in DRIVER_DEFINITIONS):
    raise RuntimeError("Every MFI driver must identify its registered parent subsection")
ALL_METRIC_DEFINITIONS = (
    *OFFICIAL_SCORE_DEFINITIONS,
    *SUBSECTION_DEFINITIONS,
    *DRIVER_DEFINITIONS,
)

_registry: dict[tuple[int, str, str], MetricDefinition] = {}
for _entry in ALL_METRIC_DEFINITIONS:
    if _entry.key in _registry:
        raise RuntimeError(f"Duplicate MFI methodology key: {_entry.key!r}")
    _registry[_entry.key] = _entry

METRIC_DEFINITIONS_BY_ID: Mapping[str, MetricDefinition] = MappingProxyType(
    {definition.metric_id: definition for definition in ALL_METRIC_DEFINITIONS}
)

OFFICIAL_FULL_SCORE_VARIABLES = tuple(
    definition.variable_name for definition in OFFICIAL_SCORE_DEFINITIONS
)
MFIR_SCORE_VARIABLE = "MFIScoreMFIr"

SUBSECTIONS_BY_DIMENSION: Mapping[str, tuple[MetricDefinition, ...]] = MappingProxyType(
    {
        dimension: tuple(
            definition
            for definition in SUBSECTION_DEFINITIONS
            if definition.dimension == dimension
        )
        for dimension in DISPLAY_DIMENSIONS
    }
)
DRIVERS_BY_DIMENSION: Mapping[str, tuple[MetricDefinition, ...]] = MappingProxyType(
    {
        dimension: tuple(
            definition
            for definition in DRIVER_DEFINITIONS
            if definition.dimension == dimension
        )
        for dimension in DISPLAY_DIMENSIONS
    }
)


def calculate_dimension_score(
    dimension: str,
    component_values: Mapping[str, float],
) -> Optional[float]:
    """Validate one stored dimension score from authoritative raw components."""
    try:
        if dimension == "Assortment":
            return float(component_values["assortment.breadth"]) + float(
                component_values["assortment.depth"]
            ) - 2.0
        if dimension == "Availability":
            total = component_values["availability.scarcity"] + component_values["availability.runout"]
            return float(total) / 12.0 * 10.0
        if dimension == "Price":
            total = component_values["price.increase"] + component_values["price.stability"]
            return float(total) / 12.0 * 10.0
        if dimension == "Resilience":
            total = (
                component_values["resilience.responsiveness"]
                + component_values["resilience.vulnerability"]
            )
            return float(total) / 8.0 * 10.0
        if dimension == "Competition":
            total = (
                component_values["competition.concentration"]
                + component_values["competition.monopoly"]
            )
            return float(total) / 12.0 * 10.0
        if dimension == "Infrastructure":
            total = (
                component_values["infrastructure.condition"]
                + component_values["infrastructure.features"]
            )
            return float(total) / 14.0 * 10.0
        if dimension == "Service":
            total = component_values["service.shopping"] + component_values["service.checkout"]
            return float(total) / 6.0 * 10.0
        if dimension == "Food Quality":
            maximum = float(component_values["quality.maximum"])
            if maximum <= 0:
                return None
            return float(component_values["quality.measure"]) / maximum * 10.0
        if dimension == "Access & Protection":
            total = (
                component_values["access_protection.access"]
                + component_values["access_protection.protection"]
            )
            return float(total) / 12.0 * 10.0
    except (KeyError, TypeError, ValueError):
        return None
    return None


def calculate_current_databridge_mfi(dimension_scores: Iterable[float]) -> Optional[float]:
    values = [float(value) for value in dimension_scores]
    if len(values) != 9:
        return None
    mean_score = sum(values) / len(values)
    minimum = min(values)
    beta = CURRENT_DATABRIDGE_BETA
    return mean_score - CURRENT_DATABRIDGE_ALPHA * (
        sqrt((mean_score - minimum) ** 2 + beta**2) - beta
    )


def within_score_tolerance(actual: float, expected: float) -> bool:
    return abs(float(actual) - float(expected)) <= SCORE_VALIDATION_ABS_TOLERANCE


if len(OFFICIAL_SCORE_DEFINITIONS) != 10:
    raise RuntimeError("The Full MFI methodology must register exactly 10 Level-1 scores.")
if len(SUBSECTION_DEFINITIONS) != 18:
    raise RuntimeError("The Full MFI methodology must register exactly 18 subsection components.")
if len(DRIVER_DEFINITIONS) != 187:
    raise RuntimeError(
        f"The Full MFI methodology must register exactly 187 drivers, got {len(DRIVER_DEFINITIONS)}."
    )
