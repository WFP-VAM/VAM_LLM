"""Strict Full MFI data preparation, validation, and typed evidence loading."""
from __future__ import annotations

import io
import math
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Optional, Union

import pandas as pd
from .input_validation import MFIInputError, clean, market_metadata, parse_collection_date, prepare_identities
from .reliable_contracts import InputFinding, ValidatedAssessment, WORKFLOW_REVISION, fingerprint

from .methodology import (
    ANALYSIS_SCHEMA_VERSION,
    DISPLAY_DIMENSIONS,
    DRIVERS_BY_DIMENSION,
    METHODOLOGY_VERSION,
    MFIR_SCORE_VARIABLE,
    OFFICIAL_FULL_SCORE_VARIABLES,
    OFFICIAL_SCORE_DEFINITIONS,
    SCORE_AUTHORITY,
    SCORE_VALIDATION_ABS_TOLERANCE,
    SUBSECTIONS_BY_DIMENSION,
    MetricDefinition,
    calculate_current_databridge_mfi,
    calculate_dimension_score,
    within_score_tolerance,
)
from .schemas import (
    MFIExcludedMarketRecord,
    MFIMethodologyWarning,
    MFIMetric,
    MFIMetricSummary,
)

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {
    "MarketName",
    "Adm0Name",
    "Adm1Name",
    "LevelID",
    "DimensionName",
    "VariableName",
    "OutputValue",
    "TradersSampleSize",
}
REQUIRED_COLLECTION_DATE_FIELDS = ("StartDate", "EndDate")

DATABRIDGES_COLUMN_MAP = {
    "applicability_status": "MetricApplicability", "applicabilityStatus": "MetricApplicability", "ApplicabilityStatus": "MetricApplicability",
    "survey_id": "SurveyID", "market_id": "MarketID", "adm0_code": "Adm0Code", "adm1_code": "Adm1Code", "adm2_code": "Adm2Code",
    **{alias: target for target in ("SurveyID", "MarketID", "Adm0Code", "Adm1Code", "Adm2Code") for alias in (target[0].lower() + target[1:], target.replace("ID", "Id"), target.lower())},
    "marketName": "MarketName",
    "market_name": "MarketName",
    "adm0Name": "Adm0Name",
    "adm0_name": "Adm0Name",
    "adm1Name": "Adm1Name",
    "adm1_name": "Adm1Name",
    "adm2Name": "Adm2Name",
    "adm2_name": "Adm2Name",
    "levelID": "LevelID",
    "levelId": "LevelID",
    "level_id": "LevelID",
    "dimensionName": "DimensionName",
    "dimension_name": "DimensionName",
    "variableName": "VariableName",
    "variable_name": "VariableName",
    "outputValue": "OutputValue",
    "output_value": "OutputValue",
    "tradersSampleSize": "TradersSampleSize",
    "traders_sample_size": "TradersSampleSize",
    "startDate": "StartDate",
    "start_date": "StartDate",
    "endDate": "EndDate",
    "end_date": "EndDate",
    "marketLatitude": "MarketLatitude",
    "market_latitude": "MarketLatitude",
    "marketLongitude": "MarketLongitude",
    "market_longitude": "MarketLongitude",
}

CSVSource = Union[BinaryIO, bytes, str, Path]


def load_mfi_from_csv(
    file_content: CSVSource,
    country_override: Optional[str] = None,
    start_date_override: Optional[str] = None,
    end_date_override: Optional[str] = None,
) -> dict[str, Any]:
    """Load a complete processed DataBridge CSV through the shared preparation path."""
    return load_mfi_from_dataframe(
        _read_full_csv(file_content),
        country_override=country_override,
        start_date_override=start_date_override,
        end_date_override=end_date_override,
    )


def load_mfi_from_dataframe(
    df: pd.DataFrame,
    country_override: Optional[str] = None,
    start_date_override: Optional[str] = None,
    end_date_override: Optional[str] = None,
) -> dict[str, Any]:
    """Build the canonical Phase-1 representation for authoritative Full MFI records."""
    prepared = _prepare_dataframe(df)
    start_date = _required_collection_date(prepared, "StartDate", start_date_override)
    end_date = _required_collection_date(prepared, "EndDate", end_date_override)
    if start_date > end_date:
        raise MFIInputError("MFI collection StartDate must not be later than EndDate")
    if not country_override and not any(clean(v) for v in prepared["Adm0Name"]):
        raise MFIInputError("MFI data requires a country or an explicit country override")
    for row in prepared.attrs.get("source_intervals") or prepared.to_dict("records"):
        start = parse_collection_date(start_date_override or row.get("StartDate"))
        end = parse_collection_date(end_date_override or row.get("EndDate"))
        if start == "Unknown" or end == "Unknown" or start > end:
            message = f"Invalid collection interval at source row {row['_source_row']}"
            raise MFIInputError(message, [InputFinding(code="invalid_collection_interval", severity="error",
                message=message, row_references=[row["_source_row"]], fields=["StartDate", "EndDate"]).model_dump()])
    source_metadata = dict(prepared.attrs)
    prepared.attrs.clear()  # pandas deep-copies attrs during every row/column projection
    included_markets, excluded_records = _classify_market_records(prepared)

    methodology_warnings: list[MFIMethodologyWarning] = []
    if excluded_records:
        methodology_warnings.append(
            MFIMethodologyWarning(
                code="mfir_records_excluded",
                message=(
                    f"Excluded {len(excluded_records)} MFIr-only market record"
                    f"{'s' if len(excluded_records) != 1 else ''}; only complete Full MFI "
                    "records are authoritative for this analysis."
                ),
            )
        )

    market_records: list[dict[str, Any]] = []
    evidence_models: dict[str, dict[str, list[MFIMetric]]] = {}
    input_findings = list(source_metadata.get("input_findings", []))
    identities = source_metadata["market_identities"]
    for market_name in included_markets:
        market_df = prepared[prepared["MarketName"] == market_name]
        rows_by_key = _rows_by_semantic_key(market_df)
        official_scores = _official_scores_for_market(rows_by_key)
        subsections, subsection_warnings = _extract_evidence_group(
            rows_by_key,
            market_name,
            (definition for definitions in SUBSECTIONS_BY_DIMENSION.values() for definition in definitions),
        )
        drivers, driver_warnings = _extract_evidence_group(
            rows_by_key,
            market_name,
            (definition for definitions in DRIVERS_BY_DIMENSION.values() for definition in definitions),
        )
        methodology_warnings.extend(subsection_warnings)
        methodology_warnings.extend(driver_warnings)

        _apply_quality_normalization(subsections)
        _validate_dimension_formulas(
            market_name,
            official_scores,
            subsections,
            methodology_warnings,
        )
        _validate_overall_formula(
            market_name,
            official_scores,
            methodology_warnings,
        )

        representative = market_df.iloc[0]
        admin1 = _clean_text(representative.get("Adm1Name")) or ""
        market_key = str(representative["_market_key"])
        traders, latitude, longitude, metadata_findings = source_metadata["market_metadata"][market_key]
        input_findings.extend(metadata_findings)
        for metric in [*subsections, *drivers]:
            if metric.validation_status in {"duplicate", "out_of_range", "formula_mismatch"} or (
                metric.parsing_status in {"empty", "nonnumeric", "nonfinite"}
                and metric.applicability not in {"not_applicable", "not_represented"}
            ):
                input_findings.append(InputFinding(
                    code=f"evidence_{metric.validation_status if metric.validation_status != 'valid' else metric.parsing_status}",
                    message=f"{market_name}: {metric.display_name} has invalid explanatory evidence ({metric.validation_status}; {metric.parsing_status}).",
                    market_key=market_key, row_references=metric.source_rows,
                    fields=["OutputValue", metric.variable_name],
                ).model_dump())
        overall = official_scores["MFI"]

        evidence_models[market_key] = {
            "subsections": subsections,
            "drivers": drivers,
        }
        market_records.append(
            {
                "market_name": market_name,
                "market_key": market_key,
                "identity": identities[market_key],
                "source_market_name": identities[market_key]["source_market_name"],
                "admin0": _clean_text(representative.get("Adm0Name")) or "",
                "admin1": admin1,
                "admin2": _clean_text(representative.get("Adm2Name")) or admin1,
                "region": admin1,
                "overall_mfi": overall,
                "dimension_scores": {
                    dimension: official_scores[dimension] for dimension in DISPLAY_DIMENSIONS
                },
                "traders_surveyed": traders,
                "latitude": latitude,
                "longitude": longitude,
            }
        )

    _apply_assessment_coverage(evidence_models, len(market_records))
    metric_summaries = _build_metric_summaries(evidence_models, len(market_records))
    for market in market_records:
        groups = evidence_models[market["market_key"]]
        market["subsections"] = _group_evidence_by_dimension(groups["subsections"])
        market["drivers"] = _group_evidence_by_dimension(groups["drivers"])

    regions = sorted({market["region"] for market in market_records if market["region"]})
    country = country_override or market_records[0]["admin0"]
    warnings = [warning.message for warning in methodology_warnings]
    warnings.extend(item["message"] for item in input_findings if item["severity"] != "info")
    validated = ValidatedAssessment(
        input_fingerprint=fingerprint(df.astype(object).where(pd.notna(df), None).to_dict("records")),
        country_identity=source_metadata["country_identity"], survey_id=source_metadata["survey_id"],
        market_identities=identities, findings=input_findings,
        included_market_keys=[m["market_key"] for m in market_records],
        excluded_market_keys=[key for key, identity in identities.items() if identity["display_label"] not in included_markets],
        official_scores={m["market_key"]: {"overall_mfi": m["overall_mfi"], "dimensions": m["dimension_scores"]} for m in market_records},
        evidence_validity={m["market_key"]: {metric.metric_id: {"validation": metric.validation_status, "parsing": metric.parsing_status, "applicability": metric.applicability}
            for group in evidence_models[m["market_key"]].values() for metric in group} for m in market_records},
        metadata_provenance={"country_override": country_override, "start_date_override": start_date_override, "end_date_override": end_date_override},
    ).model_dump()
    known_traders = sum(m["traders_surveyed"] for m in market_records if m["traders_surveyed"] is not None)
    known_count = sum(m["traders_surveyed"] is not None for m in market_records)

    return {
        "workflow_revision": WORKFLOW_REVISION,
        "validated_assessment": validated,
        "input_findings": input_findings,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "score_authority": SCORE_AUTHORITY,
        "excluded_market_records": [record.model_dump() for record in excluded_records],
        "methodology_warnings": [warning.model_dump() for warning in methodology_warnings],
        "warnings": warnings,
        "markets_data": market_records,
        "metric_summaries": metric_summaries,
        "survey_metadata": {
            "country": country,
            "collection_period": f"{start_date} to {end_date}",
            "total_traders": known_traders if known_count == len(market_records) else None,
            "known_trader_subtotal": known_traders,
            "trader_count_market_coverage": known_count,
            "total_markets": len(market_records),
            "included_full_mfi_markets": len(market_records),
            "excluded_market_records": len(excluded_records),
            "regions_covered": regions,
        },
        "country": country,
        "data_collection_start": start_date,
        "data_collection_end": end_date,
        "markets": [market["market_name"] for market in market_records],
    }


def validate_csv_structure(file_content: Union[BinaryIO, bytes]) -> dict[str, Any]:
    """Validate the complete upload using the same preparation and semantic path as loading."""
    try:
        frame = _read_full_csv(file_content)
    except Exception as exc:
        return _validation_failure(f"Failed to read CSV: {exc}")

    standardised = _standardise_columns(frame.copy())
    missing = sorted(REQUIRED_COLUMNS - set(standardised.columns))
    missing_metadata = [
        field for field in REQUIRED_COLLECTION_DATE_FIELDS if _first_date(standardised, field) == "Unknown"
    ]
    preview = _build_preview(standardised)
    errors: list[str] = []
    if missing:
        errors.append(f"Missing required columns: {', '.join(missing)}")
    if missing_metadata:
        errors.append(
            "Missing or invalid required collection metadata: " + ", ".join(missing_metadata)
        )

    loaded: Optional[dict[str, Any]] = None
    input_findings: list[dict] = []
    if not errors:
        try:
            loaded = load_mfi_from_dataframe(standardised)
        except ValueError as exc:
            errors.append(str(exc))
            input_findings = getattr(exc, "findings", [])

    has_normalized = False
    if "LevelID" in standardised.columns:
        has_normalized = bool(
            (pd.to_numeric(standardised["LevelID"], errors="coerce") == 1).any()
        )
    if not has_normalized and not any("LevelID=1" in error for error in errors):
        errors.append("No normalized scores found (LevelID=1 is required)")

    return {
        "valid": not errors,
        "missing_columns": missing,
        "missing_metadata_fields": missing_metadata,
        "has_normalized_scores": has_normalized,
        "preview": preview,
        "errors": errors,
        "warnings": loaded["warnings"] if loaded else [],
        "methodology_warnings": loaded["methodology_warnings"] if loaded else [],
        "excluded_market_records": loaded["excluded_market_records"] if loaded else [],
        "input_findings": loaded.get("input_findings", []) if loaded else input_findings,
    }


def _read_full_csv(file_content: CSVSource) -> pd.DataFrame:
    if isinstance(file_content, Path):
        return pd.read_csv(file_content, dtype=str, keep_default_na=False)
    if isinstance(file_content, str):
        return pd.read_csv(file_content, dtype=str, keep_default_na=False)
    if isinstance(file_content, bytes):
        return pd.read_csv(io.BytesIO(file_content), dtype=str, keep_default_na=False)
    if hasattr(file_content, "seek"):
        file_content.seek(0)
    return pd.read_csv(file_content, dtype=str, keep_default_na=False)


def _prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    prepared = _standardise_columns(df.copy())
    _validate_required_columns(prepared)
    if prepared.empty:
        raise ValueError("MFI data is empty.")

    for column in (
        "MarketName",
        "Adm0Name",
        "Adm1Name",
        "Adm2Name",
        "DimensionName",
        "VariableName",
    ):
        if column in prepared.columns:
            prepared[column] = prepared[column].astype("string").str.strip()

    if prepared["MarketName"].isna().any() or (prepared["MarketName"] == "").any():
        raise ValueError("MFI data contains rows without a MarketName.")
    prepared = prepare_identities(prepared)
    source_metadata = dict(prepared.attrs)
    prepared.attrs.clear()
    prepared["LevelID"] = pd.to_numeric(prepared["LevelID"], errors="coerce")
    prepared["OutputValue"] = pd.to_numeric(prepared["OutputValue"], errors="coerce")
    prepared["TradersSampleSize"] = pd.to_numeric(
        prepared["TradersSampleSize"], errors="coerce"
    )
    for column in ("MarketLatitude", "MarketLongitude"):
        if column in prepared.columns:
            prepared[column] = pd.to_numeric(prepared[column], errors="coerce")

    if prepared["MarketName"].isna().any() or (prepared["MarketName"] == "").any():
        raise ValueError("MFI data contains rows without a MarketName.")
    if "Adm2Name" not in prepared.columns:
        prepared["Adm2Name"] = prepared["Adm1Name"]
    prepared.attrs.update(source_metadata)
    return prepared


def _classify_market_records(
    df: pd.DataFrame,
) -> tuple[list[str], list[MFIExcludedMarketRecord]]:
    included: list[str] = []
    excluded: list[MFIExcludedMarketRecord] = []
    failures: list[str] = []
    findings: list[dict] = []

    for market_name in sorted(df["MarketName"].dropna().unique().tolist()):
        market_df = df[df["MarketName"] == market_name]
        level_one = market_df[market_df["LevelID"] == 1]
        observed_level_one = sorted(level_one["VariableName"].dropna().unique().tolist())
        expected_counts: dict[str, int] = {}
        for definition in OFFICIAL_SCORE_DEFINITIONS:
            expected_counts[definition.variable_name] = len(
                level_one[
                    (level_one["DimensionName"] == definition.csv_dimension)
                    & (level_one["VariableName"] == definition.variable_name)
                ]
            )

        has_full_signature = any(
            variable in set(observed_level_one) for variable in OFFICIAL_FULL_SCORE_VARIABLES
        )
        has_mfir_signature = MFIR_SCORE_VARIABLE in set(observed_level_one)
        if not has_full_signature and has_mfir_signature:
            excluded.append(
                MFIExcludedMarketRecord(
                    market_name=market_name,
                    detected_record_type="mfir_only",
                    reason="MFIr signature present without a Full MFI Level-1 signature.",
                    available_level1_variables=observed_level_one,
                    missing_level1_variables=[
                        variable
                        for variable in OFFICIAL_FULL_SCORE_VARIABLES
                        if variable not in observed_level_one
                    ],
                )
            )
            continue

        market_errors: list[str] = []
        for definition in OFFICIAL_SCORE_DEFINITIONS:
            count = expected_counts[definition.variable_name]
            if count == 0:
                market_errors.append(f"missing {definition.variable_name}")
                continue
            if count > 1:
                market_errors.append(f"duplicated {definition.variable_name}")
                continue
            value = level_one.loc[
                (level_one["DimensionName"] == definition.csv_dimension)
                & (level_one["VariableName"] == definition.variable_name),
                "OutputValue",
            ].iloc[0]
            if pd.isna(value):
                market_errors.append(f"non-numeric {definition.variable_name}")
            elif not 0.0 <= float(value) <= 10.0:
                market_errors.append(
                    f"out-of-range {definition.variable_name}={float(value)}"
                )
        if market_errors:
            failures.append(f"{market_name}: {', '.join(market_errors)}")
            findings.append(InputFinding(
                code="invalid_official_scores", severity="error", message=failures[-1],
                market_key=str(market_df.iloc[0]["_market_key"]),
                row_references=sorted({int(row) for rows in level_one["_source_rows"] for row in rows}),
                fields=["LevelID", "DimensionName", "VariableName", "OutputValue"],
            ).model_dump())
        else:
            included.append(market_name)

    if failures:
        raise MFIInputError(
            "Invalid or incomplete Full MFI Level-1 record(s): " + "; ".join(failures), findings
        )
    if not included:
        if excluded:
            raise ValueError(
                "The upload contains MFIr-only records but no valid complete Full MFI markets."
            )
        raise ValueError("The upload contains no valid complete Full MFI markets.")
    return included, excluded


def _rows_by_semantic_key(
    market_df: pd.DataFrame,
) -> dict[tuple[int, str, str], pd.DataFrame]:
    keyed: dict[tuple[int, str, str], pd.DataFrame] = {}
    usable = market_df.dropna(subset=["LevelID", "DimensionName", "VariableName"])
    for key, rows in usable.groupby(
        ["LevelID", "DimensionName", "VariableName"], sort=False, dropna=False
    ):
        level_id, dimension_name, variable_name = key
        keyed[(int(level_id), str(dimension_name), str(variable_name))] = rows
    return keyed


def _official_scores_for_market(
    rows_by_key: dict[tuple[int, str, str], pd.DataFrame],
) -> dict[str, float]:
    scores: dict[str, float] = {}
    for definition in OFFICIAL_SCORE_DEFINITIONS:
        value = rows_by_key[definition.key]["OutputValue"].iloc[0]
        scores[definition.dimension] = float(value)
    return scores


def _extract_evidence_group(
    rows_by_key: dict[tuple[int, str, str], pd.DataFrame],
    market_name: str,
    definitions: Iterable[MetricDefinition],
) -> tuple[list[MFIMetric], list[MFIMethodologyWarning]]:
    metrics: list[MFIMetric] = []
    warnings: list[MFIMethodologyWarning] = []
    for definition in definitions:
        rows = rows_by_key.get(definition.key)
        if rows is None:
            rows = pd.DataFrame(columns=["OutputValue", "TradersSampleSize"])
        count = len(rows)
        raw_value = float(rows["OutputValue"].iloc[0]) if count and pd.notna(rows["OutputValue"].iloc[0]) else None
        applicability = "available"
        validation = "valid"
        normalized: Optional[float] = None

        parse_status = str(rows["_output_state"].iloc[0]) if count and "_output_state" in rows else "valid" if raw_value is not None else "absent"
        if count == 0 or raw_value is None or not math.isfinite(raw_value):
            validation = "missing"
            applicability = "missing"
            raw_value = None
        elif count > 1:
            validation = "duplicate"
        elif not definition.raw_min <= raw_value <= definition.raw_max:
            validation = "out_of_range"
        else:
            normalized = definition.normalize(raw_value)

        explicit = {clean(v).casefold() for v in rows.get("MetricApplicability", []) if clean(v)}
        semantic_applicability = "applicable" if validation == "valid" else "unknown"
        basis = "observed" if semantic_applicability == "applicable" else "unknown"
        if len(explicit) == 1 and next(iter(explicit)) in {"applicable", "not_applicable", "not_represented"}:
            semantic_applicability = next(iter(explicit))
            basis = "explicit_source_column"
            if semantic_applicability != "applicable":
                applicability = "not_applicable"
                normalized = None
        elif len(explicit) > 1:
            validation = "duplicate"
            normalized = None

        metric = MFIMetric(
            metric_id=definition.metric_id,
            dimension=definition.dimension,
            display_name=definition.display_name,
            variable_name=definition.variable_name,
            source_level_id=definition.source_level_id,
            source_level_name=definition.source_level_name,
            role=definition.role,
            raw_value=raw_value,
            raw_min=definition.raw_min,
            raw_max=definition.raw_max,
            normalized_value=normalized,
            orientation=definition.orientation,
            unit=definition.unit,
            evidence_scope=definition.evidence_scope,
            observed_raw_values=[
                float(value) if pd.notna(value) and math.isfinite(float(value)) else None
                for value in rows["OutputValue"].tolist()
            ],
            applicability_status=applicability,
            applicability=semantic_applicability,
            validation_status=validation,
            parsing_status=parse_status,
            applicability_basis=basis,
            source_rows=sorted({int(n) for ns in rows.get("_source_rows", []) for n in ns}),
            source_values=[v for group in rows.get("_source_values", []) for v in group],
            methodology_note=definition.methodology_note,
            product_group=definition.product_group,
            question_group=definition.question_group,
            item_name=definition.item_name,
            severity_weight=definition.severity_weight,
            traders_sample_size=_nullable_int(_first_non_null(rows["TradersSampleSize"]))
            if count
            else None,
        )
        metrics.append(metric)

        should_warn = definition.role in {
            "official_subsection",
            "dimension_validation_component",
        } and validation != "valid"
        if should_warn:
            warnings.append(
                _evidence_warning(
                    market_name,
                    definition,
                    validation,
                    raw_value,
                    count,
                )
            )
        elif validation in {"duplicate", "out_of_range"} or parse_status in {"nonnumeric", "nonfinite", "empty"}:
            warnings.append(
                _evidence_warning(
                    market_name,
                    definition,
                    validation,
                    raw_value,
                    count,
                )
            )
    return metrics, warnings


def _evidence_warning(
    market_name: str,
    definition: MetricDefinition,
    validation: str,
    raw_value: Optional[float],
    count: int,
) -> MFIMethodologyWarning:
    if validation == "missing":
        detail = "is absent or non-numeric"
    elif validation == "duplicate":
        detail = f"appears {count} times"
    else:
        detail = (
            f"has value {raw_value}, outside its registered range "
            f"{definition.raw_min}–{definition.raw_max}"
        )
    return MFIMethodologyWarning(
        code=f"evidence_{validation}",
        message=(
            f"{market_name}: {definition.display_name} ({definition.variable_name}) "
            f"{detail}; the official Level-1 score remains authoritative."
        ),
        market_name=market_name,
        dimension=definition.dimension,
        metric_ids=[definition.metric_id],
        actual_value=raw_value,
    )


def _apply_quality_normalization(subsections: list[MFIMetric]) -> None:
    by_id = {metric.metric_id: metric for metric in subsections}
    measure = by_id.get("quality.measure")
    maximum = by_id.get("quality.maximum")
    if (
        measure
        and maximum
        and measure.validation_status == "valid"
        and maximum.validation_status == "valid"
        and measure.raw_value is not None
        and maximum.raw_value is not None
        and maximum.raw_value > 0
    ):
        measure.normalized_value = measure.raw_value / maximum.raw_value * 10.0


def _validate_dimension_formulas(
    market_name: str,
    official_scores: dict[str, float],
    subsections: list[MFIMetric],
    warnings: list[MFIMethodologyWarning],
) -> None:
    metrics_by_id = {metric.metric_id: metric for metric in subsections}
    for dimension in DISPLAY_DIMENSIONS:
        components = [
            metric for metric in subsections if metric.dimension == dimension
        ]
        if not components or any(
            metric.validation_status != "valid" or metric.raw_value is None
            for metric in components
        ):
            continue
        expected = calculate_dimension_score(
            dimension,
            {metric.metric_id: metric.raw_value for metric in components if metric.raw_value is not None},
        )
        if expected is None:
            for metric in components:
                metrics_by_id[metric.metric_id].validation_status = "formula_mismatch"
                metrics_by_id[metric.metric_id].normalized_value = None
            warnings.append(
                MFIMethodologyWarning(
                    code="dimension_formula_unavailable",
                    message=(
                        f"{market_name}: {dimension} subsection evidence cannot produce "
                        "a valid methodology score; the official Level-1 score remains "
                        "authoritative and the explanatory evidence was marked unusable."
                    ),
                    market_name=market_name,
                    dimension=dimension,
                    metric_ids=[metric.metric_id for metric in components],
                    actual_value=official_scores[dimension],
                    tolerance=SCORE_VALIDATION_ABS_TOLERANCE,
                )
            )
            continue
        actual = official_scores[dimension]
        if within_score_tolerance(actual, expected):
            continue
        for metric in components:
            metrics_by_id[metric.metric_id].validation_status = "formula_mismatch"
            metrics_by_id[metric.metric_id].normalized_value = None
        warnings.append(
            MFIMethodologyWarning(
                code="dimension_formula_mismatch",
                message=(
                    f"{market_name}: {dimension} subsection evidence calculates to "
                    f"{expected}, while the authoritative Level-1 score is {actual}; "
                    "the explanatory evidence was marked unusable."
                ),
                market_name=market_name,
                dimension=dimension,
                metric_ids=[metric.metric_id for metric in components],
                expected_value=expected,
                actual_value=actual,
                delta=actual - expected,
                tolerance=SCORE_VALIDATION_ABS_TOLERANCE,
            )
        )


def _validate_overall_formula(
    market_name: str,
    official_scores: dict[str, float],
    warnings: list[MFIMethodologyWarning],
) -> None:
    expected = calculate_current_databridge_mfi(
        official_scores[dimension] for dimension in DISPLAY_DIMENSIONS
    )
    actual = official_scores["MFI"]
    if expected is None or within_score_tolerance(actual, expected):
        return
    warnings.append(
        MFIMethodologyWarning(
            code="overall_formula_mismatch",
            message=(
                f"{market_name}: the current DataBridge overall formula calculates to "
                f"{expected}, while the authoritative stored MFIScoreMFI is {actual}; "
                "the stored score was retained."
            ),
            market_name=market_name,
            dimension="MFI",
            metric_ids=["mfi.overall"],
            expected_value=expected,
            actual_value=actual,
            delta=actual - expected,
            tolerance=SCORE_VALIDATION_ABS_TOLERANCE,
        )
    )


def _apply_assessment_coverage(
    evidence_models: dict[str, dict[str, list[MFIMetric]]],
    total_markets: int,
) -> None:
    all_metrics = [
        metric
        for groups in evidence_models.values()
        for group in groups.values()
        for metric in group
    ]
    coverage: dict[str, int] = defaultdict(int)
    missing: dict[str, int] = defaultdict(int)
    for metric in all_metrics:
        if metric.applicability_status == "available" and metric.validation_status == "valid":
            coverage[metric.metric_id] += 1
        elif metric.applicability_status == "missing" or (
            metric.applicability_status == "available"
            and metric.validation_status != "valid"
        ):
            missing[metric.metric_id] += 1
    for metric in all_metrics:
        metric.market_coverage = coverage[metric.metric_id]
        metric.market_coverage_total = total_markets
        metric.missing_count = missing[metric.metric_id]


def _build_metric_summaries(
    evidence_models: dict[str, dict[str, list[MFIMetric]]],
    total_markets: int,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[MFIMetric]] = defaultdict(list)
    for groups in evidence_models.values():
        for metrics in groups.values():
            for metric in metrics:
                grouped[metric.metric_id].append(metric)

    summaries: dict[str, list[dict[str, Any]]] = {
        dimension: [] for dimension in DISPLAY_DIMENSIONS
    }
    for metric_id, metrics in grouped.items():
        reference = metrics[0]
        available = [
            metric
            for metric in metrics
            if metric.applicability_status == "available"
            and metric.validation_status == "valid"
            and metric.raw_value is not None
        ]
        normalized = [
            metric.normalized_value
            for metric in available
            if metric.normalized_value is not None
        ]
        numerator = sum(metric.raw_value for metric in available if metric.raw_value is not None)
        denominator = len(available)
        missing_count = sum(
            metric.applicability_status == "missing"
            or (
                metric.applicability_status == "available"
                and metric.validation_status != "valid"
            )
            for metric in metrics
        )
        summary = MFIMetricSummary(
            metric_id=metric_id,
            dimension=reference.dimension,
            display_name=reference.display_name,
            role=reference.role,
            mean_raw_value=numerator / denominator if denominator else None,
            mean_normalized_value=(
                sum(normalized) / len(normalized) if normalized else None
            ),
            aggregation_numerator=numerator if denominator else None,
            aggregation_denominator=denominator,
            available_market_count=denominator,
            total_assessed_market_count=total_markets,
            missing_count=missing_count,
            applicability_counts={state: sum(metric.applicability == state for metric in metrics) for state in ("applicable", "not_applicable", "not_represented", "unknown")},
            unit=reference.unit,
            orientation=reference.orientation,
            evidence_scope=reference.evidence_scope,
            contributing_metric_ids=[metric_id] if denominator else [],
            methodology_note=reference.methodology_note,
        )
        summaries[reference.dimension].append(summary.model_dump())
    for dimension in summaries:
        summaries[dimension].sort(key=lambda item: item["metric_id"])
    return summaries


def _group_evidence_by_dimension(metrics: list[MFIMetric]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {
        dimension: [] for dimension in DISPLAY_DIMENSIONS
    }
    for metric in metrics:
        grouped[metric.dimension].append(metric.model_dump())
    return grouped


def _build_preview(df: pd.DataFrame) -> dict[str, Any]:
    preview: dict[str, Any] = {"columns": list(df.columns)}
    if "Adm0Name" in df.columns and not df.empty:
        preview["country"] = _clean_text(df["Adm0Name"].iloc[0])
    if "MarketName" in df.columns:
        preview["markets_count"] = int(df["MarketName"].nunique())
        preview["markets_sample"] = [
            _clean_text(value) for value in df["MarketName"].dropna().unique()[:5]
        ]
    if "Adm1Name" in df.columns:
        preview["regions_count"] = int(df["Adm1Name"].nunique())
        preview["regions"] = [
            _clean_text(value) for value in df["Adm1Name"].dropna().unique()
        ]
    if "DimensionName" in df.columns:
        preview["dimensions"] = [
            _clean_text(value) for value in df["DimensionName"].dropna().unique()
        ]
    return preview


def _validation_failure(message: str) -> dict[str, Any]:
    return {
        "valid": False,
        "missing_columns": [],
        "missing_metadata_fields": [],
        "has_normalized_scores": False,
        "preview": {},
        "errors": [message],
        "warnings": [],
        "methodology_warnings": [],
        "excluded_market_records": [],
    }


def _standardise_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {
        column: DATABRIDGES_COLUMN_MAP.get(str(column), str(column).strip())
        for column in df.columns
    }
    return df.rename(columns=rename)


def _validate_required_columns(df: pd.DataFrame) -> None:
    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise ValueError(
            f"MFI data missing required columns: {', '.join(sorted(missing))}"
        )


def _first_date(df: pd.DataFrame, column: str) -> str:
    if column not in df.columns or df[column].dropna().empty:
        return "Unknown"
    values = [_format_date(row.get(column)) for row in df.attrs.get("source_intervals", [])] if df.attrs.get("source_intervals") else [_format_date(v) for v in df[column].dropna().tolist()]
    valid = [v for v in values if v != "Unknown"]
    return (max(valid) if column == "EndDate" else min(valid)) if valid else "Unknown"


def _required_collection_date(
    df: pd.DataFrame, column: str, override: Optional[str]
) -> str:
    resolved = (
        _format_date(override)
        if override not in (None, "")
        else _first_date(df, column)
    )
    if resolved == "Unknown":
        raise ValueError(
            f"MFI data requires a valid {column} value. Upload a corrected processed "
            "CSV or supply an API override."
        )
    return resolved


def _format_date(value: Any) -> str:
    return parse_collection_date(value)


def _clean_text(value: Any) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    return str(value).strip()


def _first_non_null(values: pd.Series) -> Any:
    available = values.dropna()
    return available.iloc[0] if not available.empty else None


def _nullable_int(value: Any) -> Optional[int]:
    if value is None or pd.isna(value):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number >= 0 and number.is_integer() else None
    except (TypeError, ValueError):
        return None
