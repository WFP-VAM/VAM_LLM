"""Local dispatcher for Streamlit-only runtime."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import threading
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np
from pydantic import ValidationError

from app.shared.async_runs import (
    create_run,
    get_run,
    get_run_artifact,
    set_run_completed,
    set_run_failed,
    update_run,
    update_run_progress,
)
from app.shared.docx_export import build_content_disposition, build_docx_bytes_from_report_blocks
from app.shared.live_outputs import (
    build_databridges_live_output,
    build_document_live_output,
    create_databridges_artifacts,
    create_document_previews_with_artifacts,
)
from app.shared.report_blocks import (
    build_market_monitor_report_blocks,
    resolve_mfi_report_blocks,
)
from app.shared.countries import supported_country_options
from app.shared.llm_observability import LLMCallError, observability_config
from app.shared.llm import (
    LLMRuntimeConfigurationError,
    llm_runtime_status,
    require_llm_runtime_config,
)

from app.services.mfi_drafter.data_loader import (
    load_mfi_from_csv,
    validate_csv_structure,
)
from app.services.mfi_drafter.compatibility import (
    canonical_and_legacy_response_fields,
)
from app.services.mfi_drafter.context_status import not_attempted_context_status
from app.services.mfi_drafter.features import (
    MFIAnalysisVersionDisabled,
    mfi_release_control,
    require_mfi_analysis_v2,
)
from app.services.mfi_drafter.graph import (
    DIMENSION_DESCRIPTIONS,
    reconcile_correction_history_for_failure,
    reconcile_generation_diagnostics_for_blocked_failure,
    reconcile_generation_diagnostics_for_llm_failure,
)
from app.services.mfi_drafter.light_service import run_mfi_report_generation, runtime_status as light_runtime_status
from app.services.mfi_drafter.errors import MFIGenerationBlockedError
from app.services.mfi_drafter.schemas import MFI_DIMENSIONS
from app.services.market_monitor.graph import (
    AVAILABLE_MODULES,
    normalize_qa_review,
    run_report_generation,
)
from app.services.market_monitor.data_loader import (
    PriceCacheUnavailableError,
    check_data_availability,
    get_available_commodities,
    get_cache_status_snapshot,
    get_country_metadata as get_market_monitor_country_metadata,
    get_commodity_categories,
    get_reportable_months,
    get_supported_countries as get_market_monitor_supported_countries,
    normalize_country_name,
    refresh_reportable_months_from_databridges,
)
from app.services.market_monitor.price_backfill import PriceDataGateError
from app.services.market_monitor.basket_calculation import BasketScopeValidationError
from app.services.market_monitor.food_basket import (
    BasketNotConfigured,
    BasketRole,
    BasketSaveInput,
    BasketValidationError,
    BasketVersionConflict,
    archive_country_secondary_basket,
    attach_basket_selection_to_result,
    get_country_basket_response,
    get_country_baskets_response,
    list_country_basket_history,
    list_country_basket_role_history,
    resolve_baskets_for_report,
    save_country_basket,
    save_country_basket_role,
)
from app.services.market_monitor.features import (
    SecondBasketFeatureDisabled,
    market_monitor_second_basket_enabled,
    normalize_secondary_request,
    second_basket_feature_metadata,
)
from app.services.market_monitor.schemas import GenerateReportInput, ReportableMonthsInput

logger = logging.getLogger(__name__)


@dataclass
class LocalResponse:
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    _json_data: Any = None

    @property
    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except Exception:
            return ""

    def json(self) -> Any:
        if self._json_data is not None:
            return self._json_data
        if not self.content:
            raise ValueError("No JSON content")
        return json.loads(self.content.decode("utf-8"))


class LocalHTTPException(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def _validation_detail(exc: ValidationError) -> List[Dict[str, Any]]:
    return exc.errors(include_url=False, include_context=False)


def _validate_market_monitor_generate_input(json_body: Any) -> GenerateReportInput:
    if not isinstance(json_body, dict):
        raise LocalHTTPException(400, "Invalid JSON body")
    try:
        return GenerateReportInput.model_validate(json_body)
    except ValidationError as exc:
        raise LocalHTTPException(422, _validation_detail(exc)) from exc


def _validate_reportable_months_input(payload: Any) -> ReportableMonthsInput:
    if not isinstance(payload, dict):
        raise LocalHTTPException(400, "Invalid reportability selection")
    normalized = dict(payload)
    regions = normalized.get("admin1_list", [])
    if isinstance(regions, str):
        normalized["admin1_list"] = [regions] if regions.strip() else []
    try:
        return ReportableMonthsInput.model_validate(normalized)
    except ValidationError as exc:
        raise LocalHTTPException(422, _validation_detail(exc)) from exc


def _validate_basket_save_input(json_body: Any) -> BasketSaveInput:
    if not isinstance(json_body, dict):
        raise LocalHTTPException(400, "Invalid JSON body")
    try:
        return BasketSaveInput.model_validate(json_body)
    except ValidationError as exc:
        raise LocalHTTPException(422, _validation_detail(exc)) from exc


@dataclass
class UploadedFile:
    filename: str
    content: bytes
    content_type: Optional[str] = None


def _json_default(obj: Any) -> Any:
    if obj is None:
        return None

    if dataclasses.is_dataclass(obj):
        return asdict(obj)

    if hasattr(obj, "model_dump"):
        return obj.model_dump()

    if hasattr(obj, "dict"):
        return obj.dict()

    if hasattr(obj, "to_dict") and callable(getattr(obj, "to_dict")):
        return obj.to_dict()

    if isinstance(obj, (list, dict, str, int, float, bool)):
        return obj

    if isinstance(obj, (set, tuple)):
        return list(obj)

    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except Exception:
            return str(obj)

    iso = getattr(obj, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass

    try:
        if isinstance(obj, np.generic):
            return obj.item()
    except Exception:
        pass

    try:
        import pandas as pd  # type: ignore

        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
    except Exception:
        pass

    return str(obj)


def _normalize_payload(payload: Any) -> Any:
    try:
        return json.loads(json.dumps(payload, ensure_ascii=False, default=_json_default))
    except Exception:
        return payload


def _json_response(payload: Any, status_code: int = 200, headers: Optional[Dict[str, str]] = None) -> LocalResponse:
    normalized = _normalize_payload(payload)
    content = json.dumps(normalized, ensure_ascii=False, default=_json_default).encode("utf-8")
    final_headers = {"Content-Type": "application/json"}
    if headers:
        final_headers.update(headers)
    return LocalResponse(status_code=status_code, headers=final_headers, content=content, _json_data=normalized)


def _error_response(status_code: int, detail: Any) -> LocalResponse:
    return _json_response({"detail": detail}, status_code=status_code)


def _parse_path_and_params(path: str, params: Optional[Dict[str, Any]]) -> Tuple[List[str], Dict[str, Any]]:
    params = dict(params or {})
    path = (path or "").strip()

    if "://" in path:
        parsed = urlparse(path)
        path = parsed.path or ""
        query = parsed.query
    else:
        query = ""

    if "?" in path:
        path, query = path.split("?", 1)

    if query:
        for key, values in parse_qs(query, keep_blank_values=True).items():
            if not values:
                params.setdefault(key, "")
            elif len(values) == 1:
                params.setdefault(key, values[0])
            else:
                params.setdefault(key, values)

    if path.startswith("/api/"):
        path = path[4:]

    parts = [unquote(p) for p in path.strip("/").split("/") if p]
    return parts, params


def _get_form_value(data: Optional[Dict[str, Any]], key: str, default: Any = None) -> Any:
    if not data:
        return default
    value = data.get(key, default)
    if isinstance(value, list):
        return value[0] if value else default
    return value


def _extract_file(files: Any, key: str) -> Optional[UploadedFile]:
    if not files:
        return None

    file_item = None
    if isinstance(files, dict):
        file_item = files.get(key)
    elif isinstance(files, (list, tuple)):
        for item in files:
            if isinstance(item, tuple) and len(item) >= 2 and item[0] == key:
                file_item = item[1]
                break

    if file_item is None:
        return None

    filename = None
    content = None
    content_type = None

    if isinstance(file_item, (list, tuple)):
        if len(file_item) >= 1:
            filename = file_item[0]
        if len(file_item) >= 2:
            content = file_item[1]
        if len(file_item) >= 3:
            content_type = file_item[2]
    else:
        filename = getattr(file_item, "filename", None)
        content = file_item

    if hasattr(content, "read"):
        content = content.read()

    if content is None:
        return None

    if filename is None:
        filename = "upload"

    if isinstance(content, bytearray):
        content = bytes(content)

    if not isinstance(content, bytes):
        content = str(content).encode("utf-8")

    return UploadedFile(filename=str(filename), content=content, content_type=content_type)


def _save_temp_file(content: bytes, suffix: str) -> str:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(content)
    tmp.close()
    return tmp.name


def _trace_error(traces: List[Dict[str, Any]], retriever_name: str) -> Optional[str]:
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        if str(trace.get("retriever") or "") != retriever_name:
            continue
        error = trace.get("error")
        if error:
            return str(error)
    return None


def _update_live_metadata(
    run_id: str,
    *,
    section_updates: Optional[Dict[str, Any]] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    if not section_updates and not extra_metadata:
        return

    current_run = get_run(run_id)
    current_metadata = dict(getattr(current_run, "metadata", {}) or {})
    meta_update: Dict[str, Any] = dict(extra_metadata or {})

    if section_updates:
        live_outputs = dict(current_metadata.get("live_outputs") or {})
        live_outputs.update(section_updates)
        meta_update["live_outputs"] = live_outputs

    update_run(run_id, metadata=meta_update)


def _mfi_analysis_run_metadata(state: Dict[str, Any]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "release_control": state.get("release_control", {}),
        "generation_diagnostics": state.get("generation_diagnostics", {}),
        "context_status": state.get("context_status", {}),
        "llm_diagnostics": state.get("llm_diagnostics", {}),
        "correction_history": state.get("correction_history", []),
    }
    profile = state.get("assessment_profile")
    if not isinstance(profile, dict):
        return metadata
    metadata.update({
        "analysis_version": profile.get("analysis_version"),
        "analysis_schema_version": profile.get("analysis_schema_version"),
        "priority_dimension_names": profile.get("priority_dimension_names", []),
        "priority_market_names": profile.get("priority_market_names", []),
        "analysis_limitations": profile.get("limitations", []),
        "methodology_warnings": state.get("methodology_warnings", []),
        "narrative_schema_version": state.get("narrative_schema_version", "2.0"),
        "claim_validation": state.get("claim_validation", {}),
        "qa_review": state.get("qa_review", {}),
    })
    return metadata


def _record_mfi_llm_failure_metadata(run_id: str, error: LLMCallError) -> None:
    run = get_run(run_id)
    metadata = dict(getattr(run, "metadata", {}) or {})
    update_run(
        run_id,
        metadata={
            "generation_diagnostics": (
                reconcile_generation_diagnostics_for_llm_failure(metadata, error)
            ),
            "correction_history": reconcile_correction_history_for_failure(
                metadata, task_id=error.task_id
            ),
        },
    )


def _record_mfi_blocked_failure_metadata(
    run_id: str, error: MFIGenerationBlockedError
) -> None:
    run = get_run(run_id)
    metadata = dict(getattr(run, "metadata", {}) or {})
    update_run(
        run_id,
        metadata={
            "generation_diagnostics": (
                reconcile_generation_diagnostics_for_blocked_failure(metadata, error)
            ),
            "correction_history": reconcile_correction_history_for_failure(
                metadata, task_id=error.task_id
            ),
        },
    )


def _require_enabled_mfi_release_control():
    try:
        control = require_mfi_analysis_v2()
    except MFIAnalysisVersionDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict()) from exc
    return control


def _build_mfi_report_output(
    *,
    result: Dict[str, Any],
    run_id: str,
    country: str,
    data_collection_start: str,
    data_collection_end: str,
) -> Dict[str, Any]:
    if result.get("workflow_revision") == "mfi-light-v1":
        from app.services.mfi_drafter.light_report import public_output
        return public_output(result)
    response_fields = canonical_and_legacy_response_fields(result)

    result_for_blocks = dict(result)
    result_for_blocks["country"] = country
    result_for_blocks["data_collection_start"] = data_collection_start
    result_for_blocks["data_collection_end"] = data_collection_end

    return {
        "run_id": run_id,
        "workflow_revision": (result.get("assessment_profile") or {}).get("workflow_revision"),
        "country": country,
        "data_collection_start": data_collection_start,
        "data_collection_end": data_collection_end,
        "analysis_schema_version": result.get("analysis_schema_version", "2.0"),
        "methodology_version": result.get("methodology_version", "databridge-current"),
        "score_authority": result.get("score_authority", "synthetic_mock"),
        "release_control": (
            result.get("release_control")
            or mfi_release_control().model_dump()
        ),
        "generation_diagnostics": result.get("generation_diagnostics", {}),
        "llm_diagnostics": result.get("llm_diagnostics") or {
            "service": "mfi-drafter",
            "run_id": run_id,
        },
        "excluded_market_records": result.get("excluded_market_records", []),
        "methodology_warnings": result.get("methodology_warnings", []),
        "survey_metadata": result.get("survey_metadata", {}),
        "national_mfi": response_fields["national_mfi"],
        "risk_distribution": response_fields["risk_distribution"],
        "markets_data": response_fields["markets_data"],
        "dimension_scores": response_fields["dimension_scores"],
        "mean_mfi_across_assessed_markets": response_fields[
            "mean_mfi_across_assessed_markets"
        ],
        "assessment_profile": response_fields["assessment_profile"],
        "narrative_schema_version": result.get("narrative_schema_version", "2.0"),
        "market_score_distribution": response_fields["market_score_distribution"],
        "context_status": (
            result.get("context_status")
            or not_attempted_context_status().model_dump()
        ),
        "context_evidence": result.get("context_evidence", []),
        "dimension_narratives": result.get("dimension_narratives", {}),
        "market_narratives": result.get("market_narratives", {}),
        "executive_summary_narrative": result.get(
            "executive_summary_narrative", {}
        ),
        "claim_validation": result.get("claim_validation", {}),
        "qa_review": result.get("qa_review", {}),
        "executive_summary": response_fields["executive_summary"],
        "dimension_findings": response_fields["dimension_findings"],
        "market_recommendations": response_fields["market_recommendations"],
        "country_context": response_fields["country_context"],
        "document_references": result.get("document_references", []),
        "report_blocks": resolve_mfi_report_blocks(
            result_for_blocks,
            country=country,
            data_collection_start=data_collection_start,
            data_collection_end=data_collection_end,
        ),
        "visualizations": result.get("visualizations", {}),
        "warnings": result.get("warnings", []),
        "llm_calls": result.get("llm_calls", 0),
        "correction_attempts": result.get("correction_attempts", 0),
        "success": True,
    }


def _build_market_monitor_output(
    *,
    result: Dict[str, Any],
    run_id: str,
    country: str,
    time_period: str,
) -> Dict[str, Any]:
    result = attach_basket_selection_to_result(result, None)
    return {
        "run_id": run_id,
        "country": country,
        "time_period": time_period,
        "language": result.get("language", "en"),
        "locale": result.get("locale", "en_US"),
        "language_source": result.get("language_source", "default"),
        "report_sections": result.get("report_draft_sections", {}),
        "report_blocks": build_market_monitor_report_blocks(result),
        "visualizations": result.get("visualizations", {}),
        "data_statistics": result.get("data_statistics", {}),
        "trend_analysis": result.get("trend_analysis"),
        "events": result.get("events", []),
        "module_sections": result.get("module_sections", {}),
        "document_references": result.get("document_references", []),
        "news_counts": result.get("news_counts", {}),
        "cache_metadata": result.get("cache_metadata", {}),
        "food_basket": result.get("food_basket", {}),
        "food_baskets": result.get("food_baskets", {}),
        "basket_statistics": result.get("basket_statistics", {}),
        "basket_series_national": result.get("basket_series_national", []),
        "basket_series_regional": result.get("basket_series_regional", []),
        "secondary_basket_included": bool(result.get("secondary_basket_included", False)),
        "qa_review": normalize_qa_review(result),
        "fuel_energy_data": result.get("fuel_energy_data"),
        "livestock_animal_products_data": result.get("livestock_animal_products_data"),
        "labour_market_data": result.get("labour_market_data"),
        "warnings": result.get("warnings", []),
        "llm_calls": result.get("llm_calls", 0),
        "llm_diagnostics": result.get("llm_diagnostics") or {
            "service": "market-monitor",
            "run_id": run_id,
        },
        "success": True,
    }


def _dispatch_mfi_drafter(
    method: str,
    parts: List[str],
    *,
    json_body: Any,
    data: Optional[Dict[str, Any]],
    files: Any,
    params: Dict[str, Any],
) -> LocalResponse:
    if len(parts) == 2 and parts[0] in {"resume", "draft", "analysis", "export-draft-docx"}:
        from app.services.mfi_drafter.execution_service import get_mfi_run
        from app.services.mfi_drafter.light_service import schedule_resume
        from app.services.mfi_drafter.execution import RecoveryError
        from app.services.mfi_drafter.drafts import draft_payload, analysis_payload, export_draft
        try:
            if get_mfi_run(parts[1]) is None:
                raise RecoveryError("Run ID not found", 404)
            options = json_body if isinstance(json_body, dict) else {}
            if method == "POST" and parts[0] == "resume":
                if not isinstance(options.get("expected_revision"), int) or not str(options.get("idempotency_key") or "").strip():
                    raise RecoveryError("expected_revision and idempotency_key are required", 422)
                def schedule(function, *args):
                    threading.Thread(target=function, args=args, daemon=True).start()
                return _json_response(schedule_resume(parts[1], options["expected_revision"], options["idempotency_key"], schedule), status_code=202)
            if method == "GET" and parts[0] == "draft":
                return _json_response(draft_payload(parts[1], params.get("snapshot_revision")))
            if method == "GET" and parts[0] == "analysis":
                return _json_response(analysis_payload(parts[1]))
            if method == "POST" and parts[0] == "export-draft-docx":
                content, filename = export_draft(parts[1], options.get("snapshot_revision"))
                return LocalResponse(status_code=200, content=content, headers={"Content-Disposition": build_content_disposition(filename),
                    "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
        except RecoveryError as exc:
            raise LocalHTTPException(exc.status_code, str(exc)) from exc
    if method == "POST" and parts == ["generate"]:
        return _mfi_drafter_generate(json_body=json_body)
    if method == "POST" and parts == ["generate-from-csv"]:
        return _mfi_drafter_generate_from_csv(data=data, files=files, params=params)
    if method == "POST" and parts == ["validate-csv"]:
        return _mfi_drafter_validate_csv(files=files)
    if method == "POST" and parts == ["generate-from-csv-async"]:
        return _mfi_drafter_generate_from_csv_async(data=data, files=files, params=params)
    if method == "POST" and parts == ["generate-async"]:
        return _mfi_drafter_generate_async(json_body=json_body)
    if method == "GET" and len(parts) == 2 and parts[0] == "status":
        return _mfi_drafter_status(parts[1])
    if method == "GET" and len(parts) == 2 and parts[0] == "result":
        return _mfi_drafter_result(parts[1])
    if method == "GET" and len(parts) == 3 and parts[0] == "artifacts":
        return _mfi_drafter_artifact(parts[1], parts[2])
    if method == "POST" and len(parts) == 2 and parts[0] == "export-docx":
        return _mfi_drafter_export_docx(parts[1], json_body=json_body)
    if method == "GET" and parts == ["info"]:
        return _json_response(_mfi_drafter_info())
    if method == "GET" and parts == ["health"]:
        control = mfi_release_control()
        return _json_response(
            {
                "status": "healthy",
                "service": "mfi-drafter",
                "generation_enabled": control.enabled,
                "release_control": control.model_dump(),
                "llm_observability": observability_config().model_dump(),
                "llm_runtime": light_runtime_status(),
            }
        )
    if method == "GET" and parts == ["dimensions"]:
        return _json_response(_mfi_drafter_dimensions())
    if method == "GET" and parts == ["sample-markets"]:
        return _json_response(_mfi_drafter_sample_markets())
    raise LocalHTTPException(404, f"Unknown MFI drafter endpoint: {'/'.join(parts)}")


def _mfi_drafter_generate(*, json_body: Any) -> LocalResponse:
    release_control = _require_enabled_mfi_release_control()
    if not isinstance(json_body, dict):
        raise LocalHTTPException(400, "Invalid JSON body")

    country = json_body.get("country")
    data_collection_start = json_body.get("data_collection_start")
    data_collection_end = json_body.get("data_collection_end")
    markets = json_body.get("markets") or []
    if not isinstance(markets, list):
        markets = [markets] if markets else []

    try:
        result = run_mfi_report_generation(
            country=country,
            data_collection_start=data_collection_start,
            data_collection_end=data_collection_end,
            markets=markets,
            release_control=release_control,
        )
    except LLMCallError as exc:
        raise LocalHTTPException(502, exc.to_public_dict())
    except MFIGenerationBlockedError as exc:
        raise LocalHTTPException(exc.status_code, exc.to_public_dict())
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))

    run_id = result.get("run_id", "unknown")
    output = _build_mfi_report_output(
        result=result,
        run_id=run_id,
        country=country,
        data_collection_start=data_collection_start,
        data_collection_end=data_collection_end,
    )
    return _json_response(output)


def _mfi_drafter_generate_from_csv(
    *,
    data: Optional[Dict[str, Any]],
    files: Any,
    params: Dict[str, Any],
) -> LocalResponse:
    release_control = _require_enabled_mfi_release_control()
    upload = _extract_file(files, "file")
    if upload is None or not upload.filename:
        raise LocalHTTPException(400, "Missing filename")
    if not upload.filename.lower().endswith(".csv"):
        raise LocalHTTPException(400, "File must be a CSV")

    country_override = _get_form_value(data, "country_override", None) or _get_form_value(
        params, "country_override", None
    )
    start_override = _get_form_value(data, "data_collection_start_override", None) or _get_form_value(
        params, "data_collection_start_override", None
    )
    end_override = _get_form_value(data, "data_collection_end_override", None) or _get_form_value(
        params, "data_collection_end_override", None
    )

    try:
        csv_data = load_mfi_from_csv(
            file_content=upload.content,
            country_override=country_override,
            start_date_override=start_override,
            end_date_override=end_override,
        )
    except ValueError as exc:
        raise LocalHTTPException(400, str(exc))
    except LLMCallError as exc:
        raise LocalHTTPException(502, exc.to_public_dict())
    except MFIGenerationBlockedError as exc:
        raise LocalHTTPException(exc.status_code, exc.to_public_dict())
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))

    try:
        result = run_mfi_report_generation(
            country=csv_data["country"],
            data_collection_start=csv_data["data_collection_start"],
            data_collection_end=csv_data["data_collection_end"],
            markets=csv_data["markets"],
            csv_data=csv_data,
            release_control=release_control,
        )
    except LLMCallError as exc:
        raise LocalHTTPException(502, exc.to_public_dict())
    except MFIGenerationBlockedError as exc:
        raise LocalHTTPException(exc.status_code, exc.to_public_dict())
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))

    run_id = result.get("run_id", "unknown")
    output = _build_mfi_report_output(
        result=result,
        run_id=run_id,
        country=csv_data["country"],
        data_collection_start=csv_data["data_collection_start"],
        data_collection_end=csv_data["data_collection_end"],
    )
    return _json_response(output)


def _mfi_drafter_validate_csv(*, files: Any) -> LocalResponse:
    upload = _extract_file(files, "file")
    if upload is None or not upload.filename:
        raise LocalHTTPException(400, "Missing filename")
    if not upload.filename.lower().endswith(".csv"):
        raise LocalHTTPException(400, "File must be a CSV")

    try:
        result = validate_csv_structure(upload.content)
    except Exception as exc:
        raise LocalHTTPException(400, f"CSV validation failed: {str(exc)}")
    return _json_response(result)


def _mfi_drafter_generate_from_csv_async(
    *,
    data: Optional[Dict[str, Any]],
    files: Any,
    params: Dict[str, Any],
) -> LocalResponse:
    release_control = _require_enabled_mfi_release_control()
    upload = _extract_file(files, "file")
    if upload is None or not upload.filename:
        raise LocalHTTPException(400, "Missing filename")
    if not upload.filename.lower().endswith(".csv"):
        raise LocalHTTPException(400, "File must be a CSV")

    country_override = _get_form_value(data, "country_override", None) or _get_form_value(
        params, "country_override", None
    )
    start_override = _get_form_value(data, "data_collection_start_override", None) or _get_form_value(
        params, "data_collection_start_override", None
    )
    end_override = _get_form_value(data, "data_collection_end_override", None) or _get_form_value(
        params, "data_collection_end_override", None
    )

    try:
        csv_data = load_mfi_from_csv(
            file_content=upload.content,
            country_override=country_override,
            start_date_override=start_override,
            end_date_override=end_override,
        )
    except ValueError as exc:
        raise LocalHTTPException(400, str(exc))

    run_id = f"mfi_{uuid.uuid4().hex[:8]}"
    create_run(run_id)
    from app.services.mfi_drafter.light_service import prepare_submission
    from app.services.mfi_drafter.execution import RecoveryError
    try:
        reservation = prepare_submission(run_id, csv_data)
    except RecoveryError as exc:
        set_run_failed(run_id, error=str(exc))
        raise LocalHTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    update_run(
        run_id,
        metadata={"release_control": release_control.model_dump(), "workflow_revision": "mfi-light-v1"},
    )

    progress_map = {
        "mfi_data_agent": 10,
        "mfi_analysis": 18,
        "context_retrieval": 25,
        "context_extractor": 40,
        "mfi_graph_designer": 55,
        "dimension_drafter": 72,
        "market_recommendations_drafter": 82,
        "executive_summary_drafter": 88,
        "deterministic_claim_validator": 92,
        "semantic_review": 94,
        "consolidated_correction": 95,
        "post_correction_validator": 96,
        "corrected_claim_verification": 96,
        "finalize_qa": 97,
        "finalize_delivery": 99,
    }

    def run_in_background() -> None:
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            def on_step(node_name: str, _state: dict) -> None:
                progress = (_state.get("generation_diagnostics") or {}).get("progress_pct", progress_map.get(node_name))
                if progress is not None:
                    update_run_progress(run_id, current_node=node_name, progress_pct=progress)
                else:
                    update_run(run_id, current_node=node_name)

                meta_update: Dict[str, Any] = {}
                meta_update.update(_mfi_analysis_run_metadata(_state))
                context_counts = _state.get("context_counts")
                if isinstance(context_counts, dict):
                    meta_update["context_counts"] = context_counts

                retriever_traces = _state.get("retriever_traces")
                traces_list = retriever_traces if isinstance(retriever_traces, list) else []
                if traces_list:
                    meta_update["retriever_traces"] = traces_list

                section_updates: Dict[str, Any] = {}
                if node_name == "context_retrieval":
                    seerist_docs = _state.get("seerist_documents") or []
                    reliefweb_docs = _state.get("reliefweb_documents") or []
                    if isinstance(seerist_docs, list):
                        seerist_error = _trace_error(traces_list, "Seerist")
                        seerist_previews = create_document_previews_with_artifacts(
                            run_id=run_id,
                            service_slug="mfi-drafter",
                            source_slug="seerist",
                            documents=seerist_docs,
                        )
                        section_updates["seerist"] = build_document_live_output(
                            title="Seerist Documents",
                            summary=(
                                f"Seerist retrieval unavailable: {seerist_error}"
                                if seerist_error
                                else f"{len(seerist_docs)} Seerist documents retrieved."
                            ),
                            documents=seerist_previews,
                            status="failed" if seerist_error else "completed",
                        )
                    if isinstance(reliefweb_docs, list):
                        reliefweb_error = _trace_error(traces_list, "ReliefWeb")
                        reliefweb_previews = create_document_previews_with_artifacts(
                            run_id=run_id,
                            service_slug="mfi-drafter",
                            source_slug="reliefweb",
                            documents=reliefweb_docs,
                        )
                        section_updates["reliefweb"] = build_document_live_output(
                            title="ReliefWeb Documents",
                            summary=(
                                f"ReliefWeb retrieval unavailable: {reliefweb_error}"
                                if reliefweb_error
                                else f"{len(reliefweb_docs)} ReliefWeb documents retrieved."
                            ),
                            documents=reliefweb_previews,
                            status="failed" if reliefweb_error else "completed",
                        )

                _update_live_metadata(
                    run_id,
                    section_updates=section_updates,
                    extra_metadata=meta_update,
                )

            result = run_mfi_report_generation(
                country=csv_data["country"],
                data_collection_start=csv_data["data_collection_start"],
                data_collection_end=csv_data["data_collection_end"],
                markets=csv_data["markets"],
                csv_data=csv_data,
                on_step=on_step,
                release_control=release_control,
                run_id=run_id,
                llm_trace_sink=on_llm_trace,
                **({"execution_reservation": reservation} if reservation else {}),
            )

            update_run(run_id, warnings=result.get("warnings", []))
            set_run_completed(run_id, result=result)
        except Exception as exc:
            if isinstance(exc, LLMCallError):
                _record_mfi_llm_failure_metadata(run_id, exc)
            elif isinstance(exc, MFIGenerationBlockedError):
                _record_mfi_blocked_failure_metadata(run_id, exc)
            public_failure = isinstance(exc, (LLMCallError, MFIGenerationBlockedError))
            tb_str = None if public_failure else traceback.format_exc()
            current_node = (
                exc.node
                if public_failure
                else (get_run(run_id).current_node if get_run(run_id) is not None else None)
            )
            error = json.dumps(exc.to_public_dict(), sort_keys=True) if public_failure else str(exc)
            set_run_failed(run_id, error=error, traceback=tb_str, current_node=current_node)

    threading.Thread(target=run_in_background, daemon=True).start()
    preview = {
        "country": csv_data.get("country"),
        "markets_count": len(csv_data.get("markets") or []),
        "collection_period": (csv_data.get("survey_metadata") or {}).get("collection_period"),
    }
    return _json_response({"run_id": run_id, "status": "pending", "preview": preview})


def _mfi_drafter_generate_async(*, json_body: Any) -> LocalResponse:
    release_control = _require_enabled_mfi_release_control()
    if not isinstance(json_body, dict):
        raise LocalHTTPException(400, "Invalid JSON body")

    run_id = f"mfi_{uuid.uuid4().hex[:8]}"
    create_run(run_id)
    update_run(
        run_id,
        metadata={"release_control": release_control.model_dump()},
    )

    progress_map = {
        "mfi_data_agent": 10,
        "mfi_analysis": 18,
        "context_retrieval": 25,
        "context_extractor": 40,
        "mfi_graph_designer": 55,
        "dimension_drafter": 72,
        "market_recommendations_drafter": 82,
        "executive_summary_drafter": 88,
        "deterministic_claim_validator": 92,
        "semantic_review": 94,
        "consolidated_correction": 95,
        "post_correction_validator": 96,
        "corrected_claim_verification": 96,
        "finalize_qa": 97,
        "finalize_delivery": 99,
    }

    def run_in_background() -> None:
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            def on_step(node_name: str, _state: dict) -> None:
                progress = (_state.get("generation_diagnostics") or {}).get("progress_pct", progress_map.get(node_name))
                if progress is not None:
                    update_run_progress(run_id, current_node=node_name, progress_pct=progress)
                else:
                    update_run(run_id, current_node=node_name)

                meta_update: Dict[str, Any] = {}
                meta_update.update(_mfi_analysis_run_metadata(_state))
                context_counts = _state.get("context_counts")
                if isinstance(context_counts, dict):
                    meta_update["context_counts"] = context_counts

                retriever_traces = _state.get("retriever_traces")
                traces_list = retriever_traces if isinstance(retriever_traces, list) else []
                if traces_list:
                    meta_update["retriever_traces"] = traces_list

                section_updates: Dict[str, Any] = {}
                if node_name == "context_retrieval":
                    seerist_docs = _state.get("seerist_documents") or []
                    reliefweb_docs = _state.get("reliefweb_documents") or []
                    if isinstance(seerist_docs, list):
                        seerist_error = _trace_error(traces_list, "Seerist")
                        seerist_previews = create_document_previews_with_artifacts(
                            run_id=run_id,
                            service_slug="mfi-drafter",
                            source_slug="seerist",
                            documents=seerist_docs,
                        )
                        section_updates["seerist"] = build_document_live_output(
                            title="Seerist Documents",
                            summary=(
                                f"Seerist retrieval unavailable: {seerist_error}"
                                if seerist_error
                                else f"{len(seerist_docs)} Seerist documents retrieved."
                            ),
                            documents=seerist_previews,
                            status="failed" if seerist_error else "completed",
                        )
                    if isinstance(reliefweb_docs, list):
                        reliefweb_error = _trace_error(traces_list, "ReliefWeb")
                        reliefweb_previews = create_document_previews_with_artifacts(
                            run_id=run_id,
                            service_slug="mfi-drafter",
                            source_slug="reliefweb",
                            documents=reliefweb_docs,
                        )
                        section_updates["reliefweb"] = build_document_live_output(
                            title="ReliefWeb Documents",
                            summary=(
                                f"ReliefWeb retrieval unavailable: {reliefweb_error}"
                                if reliefweb_error
                                else f"{len(reliefweb_docs)} ReliefWeb documents retrieved."
                            ),
                            documents=reliefweb_previews,
                            status="failed" if reliefweb_error else "completed",
                        )

                _update_live_metadata(
                    run_id,
                    section_updates=section_updates,
                    extra_metadata=meta_update,
                )

            result = run_mfi_report_generation(
                country=json_body.get("country"),
                data_collection_start=json_body.get("data_collection_start"),
                data_collection_end=json_body.get("data_collection_end"),
                markets=json_body.get("markets"),
                on_step=on_step,
                release_control=release_control,
                run_id=run_id,
                llm_trace_sink=on_llm_trace,
            )

            update_run(run_id, warnings=result.get("warnings", []))
            set_run_completed(run_id, result=result)
        except Exception as exc:
            if isinstance(exc, LLMCallError):
                _record_mfi_llm_failure_metadata(run_id, exc)
            elif isinstance(exc, MFIGenerationBlockedError):
                _record_mfi_blocked_failure_metadata(run_id, exc)
            public_failure = isinstance(exc, (LLMCallError, MFIGenerationBlockedError))
            tb_str = None if public_failure else traceback.format_exc()
            current_node = (
                exc.node
                if public_failure
                else (get_run(run_id).current_node if get_run(run_id) is not None else None)
            )
            error = json.dumps(exc.to_public_dict(), sort_keys=True) if public_failure else str(exc)
            set_run_failed(run_id, error=error, traceback=tb_str, current_node=current_node)

    threading.Thread(target=run_in_background, daemon=True).start()
    return _json_response({"run_id": run_id, "status": "pending"})


def _mfi_drafter_status(run_id: str) -> LocalResponse:
    from app.services.mfi_drafter.execution_service import get_mfi_run as get_run
    from app.services.mfi_drafter.light_service import effective_contract
    from app.services.mfi_drafter.execution import execution_status
    run = get_run(run_id)
    if run is None:
        raise LocalHTTPException(404, f"Run ID not found: {run_id}")
    return _json_response(
        {
            "run_id": run_id,
            "status": run.status,
            **execution_status(run_id, runtime=effective_contract()),
            "current_node": run.current_node,
            "progress_pct": run.progress_pct,
            "warnings": run.warnings,
            "metadata": getattr(run, "metadata", {}) or {},
            "error": run.error,
            "traceback": run.traceback,
        }
    )


def _mfi_drafter_result(run_id: str) -> LocalResponse:
    from app.services.mfi_drafter.execution_service import get_mfi_run as get_run
    run = get_run(run_id)
    if run is None:
        raise LocalHTTPException(404, f"Run ID not found: {run_id}")
    if run.status != "completed":
        raise LocalHTTPException(400, f"Report not completed. Current status: {run.status}")

    result = run.result or {}
    country = result.get("country", "Unknown")
    data_collection_start = result.get("data_collection_start", "Unknown")
    data_collection_end = result.get("data_collection_end", "Unknown")

    output = _build_mfi_report_output(
        result=result,
        run_id=run_id,
        country=country,
        data_collection_start=data_collection_start,
        data_collection_end=data_collection_end,
    )
    return _json_response(output)


def _mfi_drafter_artifact(run_id: str, artifact_id: str) -> LocalResponse:
    artifact = get_run_artifact(run_id, artifact_id)
    if artifact is None:
        raise LocalHTTPException(404, f"Artifact not found: {artifact_id}")
    headers = {
        "Content-Disposition": build_content_disposition(artifact.file_name),
        "Content-Type": artifact.mime_type,
    }
    return LocalResponse(status_code=200, headers=headers, content=artifact.content)


def _mfi_drafter_export_docx(run_id: str, *, json_body: Any) -> LocalResponse:
    from app.services.mfi_drafter.execution_service import get_mfi_run as get_run
    run = get_run(run_id)
    if run is None:
        raise LocalHTTPException(404, f"Run ID not found: {run_id}")
    if run.status != "completed":
        raise LocalHTTPException(409, f"Run not completed. Current status: {run.status}")

    options = json_body if isinstance(json_body, dict) else {}
    filename = options.get("filename") or f"mfi-drafter-{run_id}.docx"
    include_sources = bool(options.get("include_sources", True))
    include_visualizations = bool(options.get("include_visualizations", True))

    result = run.result or {}
    try:
        report_blocks = resolve_mfi_report_blocks(result)
        docx_bytes = build_docx_bytes_from_report_blocks(
            report_blocks,
            visualizations=result.get("visualizations", {}),
            include_sources=include_sources,
            include_visualizations=include_visualizations,
        )
    except Exception as exc:
        raise LocalHTTPException(500, f"DOCX generation failed: {str(exc)}")

    headers = {
        "Content-Disposition": build_content_disposition(filename),
        "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    logger.info(
        "MFI Drafter DOCX export completed",
        extra={
            "mfi_event": "docx_export_completed",
            "mfi_run_id": run_id,
            "mfi_docx_bytes": len(docx_bytes),
        },
    )
    return LocalResponse(status_code=200, headers=headers, content=docx_bytes)


def _mfi_drafter_info() -> Dict[str, Any]:
    release_control = mfi_release_control()
    return {
        "id": "mfi-drafter",
        "name": "MFI Report Generator",
        "description": "Generates full Market Functionality Index (MFI) reports. "
        "Analyzes 9 market functionality dimensions and generates "
        "visualizations, an executive summary, and recommendations.",
        "version": "2.0.0",
        "release_control": release_control.model_dump(),
        "generation_enabled": release_control.enabled,
        "llm_observability": observability_config().model_dump(),
        "llm_runtime": light_runtime_status(),
        "supports_csv_upload": True,
        "data_source": "Uploaded processed MFI CSV",
        "csv_upload": {
            "endpoint": "/generate-from-csv",
            "async_endpoint": "/generate-from-csv-async",
            "validate_endpoint": "/validate-csv",
            "required_columns": [
                "MarketName",
                "Adm0Name",
                "Adm1Name",
                "LevelID",
                "DimensionName",
                "VariableName",
                "OutputValue",
                "TradersSampleSize",
            ],
            "optional_columns": [
                "MarketLatitude",
                "MarketLongitude",
                "Adm2Name",
                "StartDate",
                "EndDate",
            ],
            "description": "Upload the final processed or elaborated MFI CSV to generate the report.",
        },
        "outputs": {
            "run_id": "Unique generation identifier",
            "release_control": "Immutable Phase 4 deployment-control snapshot",
            "generation_diagnostics": (
                "Drafting provenance, retriever status, fallback use, and QA counts"
            ),
            "mean_mfi_across_assessed_markets": (
                "Unrounded unweighted mean across included Full MFI markets"
            ),
            "assessment_profile": (
                "Versioned deterministic profiles, rankings, limitations, "
                "ledger, and tables"
            ),
            "narrative_schema_version": "Version of the structured narrative contract",
            "market_score_distribution": "Neutral ordered assessed-market scores",
            "context_evidence": "Classified, source-linked contextual statements",
            "context_status": "Stable retrieval, classification, and accepted-context status",
            "dimension_narratives": "Metric-cited structured dimension narratives",
            "market_narratives": "Metric-cited structured market narratives",
            "executive_summary_narrative": "Metric-cited structured executive summary",
            "claim_validation": "Deterministic narrative claim validation",
            "qa_review": "Combined deterministic and Red-Team QA status",
            "national_mfi": "Deprecated Phase 4 compatibility alias",
            "risk_distribution": "Deprecated Phase 4 compatibility alias",
            "markets_data": "Detailed data for each market",
            "dimension_scores": "Score for each MFI dimension",
            "executive_summary": "Generated executive summary",
            "dimension_findings": "Findings for each dimension",
            "market_recommendations": "Recommendations by market",
            "visualizations": "Charts in Base64 format",
            "llm_calls": "Number of LLM calls performed",
            "success": "True if generation is completed",
        },
        "workflow_nodes": [
            {"id": "mfi_data_agent", "name": "MFI Data Agent", "description": "Retrieves/generates MFI data"},
            {"id": "mfi_analysis", "name": "MFI Analysis", "description": "Builds the deterministic assessment profile"},
            {"id": "context_retrieval", "name": "Context Retrieval", "description": "Retrieves contextual news"},
            {"id": "context_extractor", "name": "Context Extractor", "description": "Extracts context with the LLM"},
            {"id": "mfi_graph_designer", "name": "Graph Designer", "description": "Generates visualizations"},
            {"id": "dimension_drafter", "name": "Dimension Drafter", "description": "Drafts findings per dimension"},
            {
                "id": "market_recommendations_drafter",
                "name": "Market Recommendations",
                "description": "Drafts recommendations by market",
            },
            {
                "id": "executive_summary_drafter",
                "name": "Executive Summary",
                "description": "Drafts executive summary",
            },
            {"id": "deterministic_claim_validator", "name": "Claim Validator", "description": "Validates every claim against the closed catalog"},
            {"id": "semantic_review", "name": "Semantic Review", "description": "Runs the three bounded narrative reviews"},
            {"id": "consolidated_correction", "name": "Consolidated Correction", "description": "Repairs all material fields in one call"},
            {"id": "post_correction_validator", "name": "Post-correction Validation", "description": "Revalidates numbers and citations"},
            {"id": "corrected_claim_verification", "name": "Correction Verification", "description": "Reviews only corrected claims"},
            {"id": "finalize_qa", "name": "Finalize QA", "description": "Finalizes claim and QA status"},
            {"id": "finalize_delivery", "name": "Validate Delivery", "description": "Validates and stores reader-facing report blocks"},
        ],
        "mfi_dimensions": MFI_DIMENSIONS,
    }


def _mfi_drafter_dimensions() -> Dict[str, Any]:
    return {
        "dimensions": [
            {
                "name": dim,
                "description": DIMENSION_DESCRIPTIONS.get(dim, ""),
                "score_range": "0-10",
                "orientation": "higher_is_better",
            }
            for dim in MFI_DIMENSIONS
        ]
    }


def _mfi_drafter_sample_markets() -> Dict[str, Any]:
    return {
        "Ghana": {
            "markets": [
                "Gushegu",
                "Karaga",
                "Nanton",
                "Sang",
                "Tamale Aboabo",
                "Yendi",
                "Fumbisi",
                "Bussie",
                "Gwollu",
                "Nyoli",
                "Tangasie",
                "Tumu",
            ]
        },
        "Sudan": {
            "markets": [
                "Omdurman",
                "Khartoum Central",
                "El Fasher",
                "Nyala",
                "Kassala City",
                "Gedaref",
                "Port Sudan",
            ]
        },
        "Yemen": {
            "markets": [
                "Sana'a Central",
                "Aden Port",
                "Taiz City",
                "Hodeidah",
                "Mukalla",
                "Ibb",
            ]
        },
    }


def _dispatch_market_monitor(
    method: str,
    parts: List[str],
    *,
    json_body: Any,
    data: Optional[Dict[str, Any]],
    files: Any,
    params: Dict[str, Any],
) -> LocalResponse:
    if method == "POST" and parts == ["generate"]:
        return _market_monitor_generate(json_body=json_body)
    if method == "POST" and parts == ["generate-async"]:
        return _market_monitor_generate_async(json_body=json_body)
    if method == "GET" and parts == ["data-availability"]:
        return _market_monitor_data_availability(params=params)
    if method == "GET" and parts == ["cache", "status"]:
        return _json_response(get_cache_status_snapshot())
    if method == "GET" and len(parts) == 2 and parts[0] == "status":
        return _market_monitor_status(parts[1])
    if method == "GET" and len(parts) == 2 and parts[0] == "result":
        return _market_monitor_result(parts[1])
    if method == "GET" and len(parts) == 3 and parts[0] == "artifacts":
        return _market_monitor_artifact(parts[1], parts[2])
    if method == "POST" and len(parts) == 2 and parts[0] == "export-docx":
        return _market_monitor_export_docx(parts[1], json_body=json_body)
    if method == "GET" and parts == ["info"]:
        return _json_response(_market_monitor_info())
    if method == "GET" and parts == ["health"]:
        return _json_response(
            {
                "status": "healthy",
                "service": "market-monitor",
                "llm_observability": observability_config().model_dump(),
                "llm_runtime": llm_runtime_status().model_dump(),
            }
        )
    if method == "GET" and parts == ["countries"]:
        return _market_monitor_countries()
    if method == "GET" and parts == ["commodities"]:
        return _market_monitor_commodities(params=params)
    if method == "GET" and len(parts) == 3 and parts[0] == "countries" and parts[2] == "metadata":
        return _market_monitor_country_metadata(parts[1])
    if (
        method == "GET"
        and len(parts) == 3
        and parts[0] == "countries"
        and parts[2] == "reportable-months"
    ):
        return _market_monitor_country_reportable_months(parts[1], params=params)
    if (
        method == "POST"
        and len(parts) == 4
        and parts[0] == "countries"
        and parts[2] == "reportable-months"
        and parts[3] == "refresh"
    ):
        return _market_monitor_refresh_country_reportable_months(parts[1], json_body=json_body)
    if method == "GET" and len(parts) == 3 and parts[0] == "countries" and parts[2] == "baskets":
        return _market_monitor_country_baskets(parts[1])
    if (
        method == "POST"
        and len(parts) == 4
        and parts[0] == "countries"
        and parts[2] == "baskets"
        and parts[3] in {BasketRole.PRIMARY.value, BasketRole.SECONDARY.value}
    ):
        return _market_monitor_save_country_basket_role(
            parts[1],
            role=parts[3],
            json_body=json_body,
        )
    if (
        method == "GET"
        and len(parts) == 5
        and parts[0] == "countries"
        and parts[2] == "baskets"
        and parts[4] == "history"
    ):
        return _market_monitor_country_basket_role_history(
            parts[1],
            role=parts[3],
            params=params,
        )
    if (
        method == "DELETE"
        and len(parts) == 4
        and parts[0] == "countries"
        and parts[2] == "baskets"
        and parts[3] == BasketRole.SECONDARY.value
    ):
        return _market_monitor_archive_country_secondary_basket(parts[1])
    if method == "GET" and len(parts) == 3 and parts[0] == "countries" and parts[2] == "basket":
        return _market_monitor_country_basket(parts[1])
    if method == "POST" and len(parts) == 3 and parts[0] == "countries" and parts[2] == "basket":
        return _market_monitor_save_country_basket(parts[1], json_body=json_body)
    if method == "GET" and len(parts) == 4 and parts[0] == "countries" and parts[2] == "basket" and parts[3] == "history":
        return _market_monitor_country_basket_history(parts[1], params=params)
    raise LocalHTTPException(404, f"Unknown Market Monitor endpoint: {'/'.join(parts)}")


def _market_monitor_generate(*, json_body: Any) -> LocalResponse:
    input_data = _validate_market_monitor_generate_input(json_body)

    country = input_data.country
    time_period = input_data.time_period
    commodity_list = input_data.commodity_list
    admin1_list = input_data.admin1_list
    currency_code = input_data.currency_code
    enabled_modules = input_data.enabled_modules
    previous_report_text = input_data.previous_report_text
    use_mock_data = input_data.use_mock_data

    if not admin1_list and use_mock_data:
        admin1_list = [f"{country} North", f"{country} South", f"{country} Central"]

    try:
        include_secondary_basket, secondary_basket_version_id = normalize_secondary_request(input_data)
        basket_selection = None
        if not use_mock_data:
            basket_selection = resolve_baskets_for_report(
                country,
                primary_basket_version_id=input_data.effective_primary_basket_version_id,
                include_secondary_basket=include_secondary_basket,
                secondary_basket_version_id=secondary_basket_version_id,
            )
        result = run_report_generation(
            country=country,
            time_period=time_period,
            commodity_list=commodity_list,
            admin1_list=admin1_list,
            currency_code=currency_code,
            enabled_modules=enabled_modules,
            previous_report_text=previous_report_text,
            use_mock_data=use_mock_data,
            basket_version_id=(
                basket_selection.primary_basket_version_id
                if basket_selection is not None
                else input_data.effective_primary_basket_version_id
            ),
            basket_selection=basket_selection,
            language=input_data.language,
        )
        result = attach_basket_selection_to_result(result, basket_selection)
    except BasketVersionConflict as exc:
        raise LocalHTTPException(409, str(exc))
    except SecondBasketFeatureDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except BasketNotConfigured as exc:
        raise LocalHTTPException(409, str(exc))
    except (BasketValidationError, BasketScopeValidationError) as exc:
        raise LocalHTTPException(400, str(exc))
    except PriceDataGateError as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except LLMCallError as exc:
        raise LocalHTTPException(502, exc.to_public_dict())
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))

    run_id = result.get("run_id", "unknown")
    output = _build_market_monitor_output(
        result=result,
        run_id=run_id,
        country=country,
        time_period=time_period,
    )
    return _json_response(output)


def _market_monitor_generate_async(*, json_body: Any) -> LocalResponse:
    input_data = _validate_market_monitor_generate_input(json_body)

    submission_feature_flags = second_basket_feature_metadata()
    basket_selection = None
    try:
        include_secondary_basket, secondary_basket_version_id = normalize_secondary_request(
            input_data,
            enabled=submission_feature_flags["second_food_basket_enabled"],
        )
        if not input_data.use_mock_data:
            basket_selection = resolve_baskets_for_report(
                input_data.country,
                primary_basket_version_id=input_data.effective_primary_basket_version_id,
                include_secondary_basket=include_secondary_basket,
                secondary_basket_version_id=secondary_basket_version_id,
            )
    except SecondBasketFeatureDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except BasketVersionConflict as exc:
        raise LocalHTTPException(409, str(exc))
    except BasketNotConfigured as exc:
        raise LocalHTTPException(409, str(exc))
    except BasketValidationError as exc:
        raise LocalHTTPException(400, str(exc))

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    create_run(run_id)
    initial_metadata: Dict[str, Any] = {"feature_flags": submission_feature_flags}
    if basket_selection is not None:
        initial_metadata["basket_selection"] = basket_selection.to_metadata()
    update_run(run_id, metadata=initial_metadata)

    progress_map = {
        "data_agent": 10,
        "graph_designer": 20,
        "news_retrieval": 30,
        "event_mapper": 40,
        "trend_analyst": 55,
        "module_orchestrator": 65,
        "highlights_drafter": 75,
        "narrative_drafter": 85,
        "red_team": 95,
    }

    def run_in_background() -> None:
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            run_basket_selection = basket_selection
            if not input_data.use_mock_data and basket_selection is not None:
                # Revalidate active IDs, then retain the immutable snapshots
                # captured when the run was accepted.
                resolve_baskets_for_report(
                    input_data.country,
                    primary_basket_version_id=basket_selection.primary_basket_version_id,
                    include_secondary_basket=basket_selection.secondary_basket_included,
                    secondary_basket_version_id=basket_selection.secondary_basket_version_id,
                )
                update_run(
                    run_id,
                    metadata={"basket_selection": basket_selection.to_metadata()},
                )

            admin1_list = input_data.admin1_list
            if not admin1_list and input_data.use_mock_data:
                country = input_data.country
                admin1_list = [f"{country} North", f"{country} South"]

            def on_step(node_name: str, _state: dict) -> None:
                progress = progress_map.get(node_name)
                if progress is not None:
                    update_run_progress(run_id, current_node=node_name, progress_pct=progress)
                else:
                    update_run(run_id, current_node=node_name)

                meta_update: Dict[str, Any] = {}
                news_counts = _state.get("news_counts")
                if isinstance(news_counts, dict):
                    meta_update["news_counts"] = news_counts
                cache_metadata = _state.get("cache_metadata")
                if isinstance(cache_metadata, dict) and cache_metadata:
                    meta_update["cache_metadata"] = cache_metadata

                retriever_traces = _state.get("retriever_traces")
                traces_list = retriever_traces if isinstance(retriever_traces, list) else []
                if traces_list:
                    meta_update["retriever_traces"] = traces_list

                section_updates: Dict[str, Any] = {}
                if node_name == "data_agent":
                    rows = _state.get("databridges_rows") or []
                    if isinstance(rows, list) and rows:
                        artifacts = create_databridges_artifacts(
                            run_id=run_id,
                            service_slug="market-monitor",
                            label_prefix="Price data rows",
                            file_stem=f"market-monitor-price-data-{input_data.country}-{input_data.time_period}",
                            rows=rows,
                        )
                        section_updates["databridges"] = build_databridges_live_output(
                            title="Price Data",
                            summary=(
                                f"{len(rows)} price row(s) retrieved for "
                                f"{input_data.country} ({input_data.time_period}) "
                                "from cache plus any targeted backfill."
                            ),
                            rows=rows,
                            download_artifacts=artifacts,
                        )
                    elif input_data.use_mock_data:
                        section_updates["databridges"] = build_databridges_live_output(
                            title="Price Data",
                            summary="Mock data is enabled for this run, so no price rows were read.",
                            rows=[],
                            download_artifacts=[],
                            status="skipped",
                        )

                if node_name == "news_retrieval":
                    seerist_docs = _state.get("seerist_documents") or []
                    reliefweb_docs = _state.get("reliefweb_documents") or []
                    if isinstance(seerist_docs, list):
                        seerist_error = _trace_error(traces_list, "Seerist")
                        seerist_previews = create_document_previews_with_artifacts(
                            run_id=run_id,
                            service_slug="market-monitor",
                            source_slug="seerist",
                            documents=seerist_docs,
                        )
                        section_updates["seerist"] = build_document_live_output(
                            title="Seerist Documents",
                            summary=(
                                f"Seerist retrieval unavailable: {seerist_error}"
                                if seerist_error
                                else f"{len(seerist_docs)} Seerist documents retrieved."
                            ),
                            documents=seerist_previews,
                            status="failed" if seerist_error else "completed",
                        )
                    if isinstance(reliefweb_docs, list):
                        reliefweb_error = _trace_error(traces_list, "ReliefWeb")
                        reliefweb_previews = create_document_previews_with_artifacts(
                            run_id=run_id,
                            service_slug="market-monitor",
                            source_slug="reliefweb",
                            documents=reliefweb_docs,
                        )
                        section_updates["reliefweb"] = build_document_live_output(
                            title="ReliefWeb Documents",
                            summary=(
                                f"ReliefWeb retrieval unavailable: {reliefweb_error}"
                                if reliefweb_error
                                else f"{len(reliefweb_docs)} ReliefWeb documents retrieved."
                            ),
                            documents=reliefweb_previews,
                            status="failed" if reliefweb_error else "completed",
                        )

                _update_live_metadata(
                    run_id,
                    section_updates=section_updates,
                    extra_metadata=meta_update,
                )

            result = run_report_generation(
                country=input_data.country,
                time_period=input_data.time_period,
                commodity_list=input_data.commodity_list,
                admin1_list=admin1_list,
                currency_code=input_data.currency_code,
                enabled_modules=input_data.enabled_modules,
                previous_report_text=input_data.previous_report_text,
                use_mock_data=input_data.use_mock_data,
                basket_version_id=(
                    run_basket_selection.primary_basket_version_id
                    if run_basket_selection is not None
                    else input_data.effective_primary_basket_version_id
                ),
                basket_selection=run_basket_selection,
                language=input_data.language,
                on_step=on_step,
                run_id=run_id,
                llm_trace_sink=on_llm_trace,
            )

            result = attach_basket_selection_to_result(result, run_basket_selection)

            update_run(
                run_id,
                warnings=result.get("warnings", []),
                metadata={
                    "basket_calculation": {
                        "time_period": input_data.time_period,
                        "cache_version_id": (result.get("cache_metadata") or {}).get("cache_version_id"),
                        "specs": (result.get("cache_metadata") or {}).get("basket_calculation_specs") or [],
                        "applicable_regions": (result.get("cache_metadata") or {}).get("basket_applicable_regions") or {},
                        "coverage": (result.get("cache_metadata") or {}).get("basket_coverage") or {},
                        "series_national": result.get("basket_series_national") or [],
                        "series_regional": result.get("basket_series_regional") or [],
                        "statistics": result.get("basket_statistics") or {"primary": None, "secondary": None},
                    },
                    "qa_review": normalize_qa_review(result),
                },
            )
            set_run_completed(run_id, result=result)
        except Exception as exc:
            tb_str = None if isinstance(exc, LLMCallError) else traceback.format_exc()
            current_node = (
                exc.node
                if isinstance(exc, LLMCallError)
                else (get_run(run_id).current_node if get_run(run_id) is not None else None)
            )
            if isinstance(exc, PriceDataGateError):
                update_run(run_id, metadata={"price_gap_report": exc.gap_report.to_dict()})
            error = json.dumps(exc.to_public_dict(), sort_keys=True) if isinstance(exc, LLMCallError) else str(exc)
            set_run_failed(run_id, error=error, traceback=tb_str, current_node=current_node)

    threading.Thread(target=run_in_background, daemon=True).start()
    return _json_response({"run_id": run_id, "status": "pending"})


def _market_monitor_data_availability(*, params: Dict[str, Any]) -> LocalResponse:
    country = _get_form_value(params, "country", "")
    time_period = _get_form_value(params, "time_period", "2025-01")
    commodities_raw = _get_form_value(params, "commodities", "Sugar,Wheat flour")

    commodity_list = [c.strip() for c in str(commodities_raw).split(",") if c.strip()]

    availability = check_data_availability(
        country=str(country),
        time_period=str(time_period),
        commodities=commodity_list,
    )
    return _json_response(availability)


def _market_monitor_status(run_id: str) -> LocalResponse:
    run = get_run(run_id)
    if run is None:
        raise LocalHTTPException(404, f"Run ID not found: {run_id}")
    return _json_response(
        {
            "run_id": run_id,
            "status": run.status,
            "current_node": run.current_node,
            "progress_pct": run.progress_pct,
            "warnings": run.warnings,
            "metadata": getattr(run, "metadata", {}) or {},
            "error": run.error,
            "traceback": run.traceback,
        }
    )


def _market_monitor_result(run_id: str) -> LocalResponse:
    run = get_run(run_id)
    if run is None:
        raise LocalHTTPException(404, f"Run ID not found: {run_id}")
    if run.status != "completed":
        raise LocalHTTPException(400, f"Report not completed. Current status: {run.status}")

    result = run.result or {}
    if not result.get("warnings") and run.warnings:
        result = {**result, "warnings": run.warnings}

    output = _build_market_monitor_output(
        result=result,
        run_id=run_id,
        country=result.get("country", "Unknown"),
        time_period=result.get("time_period", "Unknown"),
    )
    return _json_response(output)


def _market_monitor_artifact(run_id: str, artifact_id: str) -> LocalResponse:
    artifact = get_run_artifact(run_id, artifact_id)
    if artifact is None:
        raise LocalHTTPException(404, f"Artifact not found: {artifact_id}")
    headers = {
        "Content-Disposition": build_content_disposition(artifact.file_name),
        "Content-Type": artifact.mime_type,
    }
    return LocalResponse(status_code=200, headers=headers, content=artifact.content)


def _market_monitor_export_docx(run_id: str, *, json_body: Any) -> LocalResponse:
    run = get_run(run_id)
    if run is None:
        raise LocalHTTPException(404, f"Run ID not found: {run_id}")
    if run.status != "completed":
        raise LocalHTTPException(409, f"Run not completed. Current status: {run.status}")

    options = json_body if isinstance(json_body, dict) else {}
    filename = options.get("filename") or f"market-monitor-{run_id}.docx"
    include_sources = bool(options.get("include_sources", True))
    include_visualizations = bool(options.get("include_visualizations", True))

    result = run.result or {}

    try:
        report_blocks = build_market_monitor_report_blocks(result)
        docx_bytes = build_docx_bytes_from_report_blocks(
            report_blocks,
            visualizations=result.get("visualizations", {}),
            include_sources=include_sources,
            include_visualizations=include_visualizations,
        )
    except Exception as exc:
        raise LocalHTTPException(500, f"DOCX generation failed: {str(exc)}")

    headers = {
        "Content-Disposition": build_content_disposition(filename),
        "Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    return LocalResponse(status_code=200, headers=headers, content=docx_bytes)


def _market_monitor_info() -> Dict[str, Any]:
    second_basket_enabled = market_monitor_second_basket_enabled()
    return {
        "id": "market-monitor",
        "name": "Market Monitor Generator",
        "description": "Generates full Market Monitor reports with price analysis, "
        "market trend analysis, visualizations, and narrative sections. "
        "Includes optional modules such as exchange rate analysis.",
        "version": "1.0.0",
        "llm_observability": observability_config().model_dump(),
        "llm_runtime": llm_runtime_status().model_dump(),
        "features": {
            "second_food_basket": {
                "enabled": second_basket_enabled,
                "environment_variable": "MARKET_MONITOR_SECOND_BASKET_ENABLED",
                "default_enabled": True,
                "disabled_behavior": (
                    "New secondary configuration and selection are disabled; existing history and completed "
                    "results remain readable and exportable."
                ),
            }
        },
        "inputs": [
            {
                "name": "country",
                "type": "string",
                "required": True,
                "label": "Country",
                "description": "Country name (e.g., 'Sudan', 'Yemen', 'Myanmar')",
            },
            {
                "name": "time_period",
                "type": "string",
                "required": True,
                "label": "Time Period",
                "description": "Period in YYYY-MM format (e.g., '2025-01')",
            },
            {
                "name": "commodity_list",
                "type": "array",
                "required": False,
                "label": "Additional commodities",
                "description": "Optional commodities to analyze in addition to the active country food basket. Basket commodities are always included.",
                "default": [],
                "note": "Query /countries/{country}/baskets for active baskets and /countries/{country}/metadata for available additional commodities.",
            },
            {
                "name": "basket_version_id",
                "type": "string",
                "required": False,
                "label": "Basket Version ID",
                "description": "Deprecated alias for primary_basket_version_id.",
            },
            {
                "name": "primary_basket_version_id",
                "type": "string",
                "required": False,
                "label": "Primary Basket Version ID",
                "description": "Optional active primary basket version guard. Stale versions return a conflict.",
            },
            {
                "name": "include_secondary_basket",
                "type": "boolean",
                "required": False,
                "label": "Include Secondary Basket",
                "description": "Include the active secondary basket when one exists.",
                "default": True,
            },
            {
                "name": "secondary_basket_version_id",
                "type": "string",
                "required": False,
                "label": "Secondary Basket Version ID",
                "description": "Optional active secondary basket version guard. Ignored when inclusion is false.",
            },
            {
                "name": "admin1_list",
                "type": "array",
                "required": False,
                "label": "Regions (Admin1)",
                "description": "List of regions to include",
            },
            {
                "name": "currency_code",
                "type": "string",
                "required": False,
                "label": "Currency Code",
                "description": "ISO 4217 currency code (e.g., 'SDG', 'YER')",
                "default": "USD",
            },
            {
                "name": "enabled_modules",
                "type": "array",
                "required": False,
                "label": "Optional Modules",
                "description": (
                    "Optional modules to enable (exchange_rate uses DataBridges FX; fuel_energy uses transport fuel "
                    "price series; livestock_animal_products uses animal product prices; labour_market uses wages)"
                ),
                "default": [],
                "options": list(AVAILABLE_MODULES.keys()),
            },
        ],
        "outputs": {
            "run_id": "Unique generation identifier",
            "report_sections": "Report sections (HIGHLIGHTS, MARKET_OVERVIEW, etc.)",
            "visualizations": "Charts in Base64 format",
            "data_statistics": "Computed statistics (MoM, YoY)",
            "food_baskets": "Resolved immutable primary and included secondary basket snapshots",
            "basket_statistics": "Independent role-keyed basket coverage, cost, MoM, YoY, and contributions",
            "basket_series_national": "Complete-component national basket series by immutable role/version",
            "basket_series_regional": "Complete-component regional basket series by immutable role/version",
            "secondary_basket_included": "Whether a secondary basket was selected for the run",
            "qa_review": "QA status, correction-attempt count, and final structured unresolved flags",
            "trend_analysis": "Market trend analysis",
            "events": "Events extracted from news",
            "module_sections": "Sections generated by optional modules",
            "fuel_energy_data": "Resolved transport fuel series and statistics when fuel_energy is enabled",
            "livestock_animal_products_data": (
                "Resolved livestock and animal product series and statistics when livestock_animal_products is enabled"
            ),
            "labour_market_data": "Resolved wage and purchasing-power series when labour_market is enabled",
            "llm_calls": "Number of LLM calls performed",
            "success": "True if generation completed successfully",
        },
        "basket_visualizations": {
            "canonical": [
                "food_basket_trend_primary",
                "food_basket_trend_secondary",
                "regional_comparison_primary",
                "regional_comparison_secondary",
            ],
            "primary_aliases": {
                "food_basket_trend": "food_basket_trend_primary",
                "regional_comparison": "regional_comparison_primary",
            },
            "combined_chart": False,
        },
        "basket_endpoints": [
            "GET /countries/{country}/baskets",
            "POST /countries/{country}/baskets/primary",
            "POST /countries/{country}/baskets/secondary",
            "GET /countries/{country}/baskets/{role}/history",
            "DELETE /countries/{country}/baskets/secondary",
            "GET /countries/{country}/reportable-months?primary_basket_version_id=...&include_secondary_basket=...",
            "POST /countries/{country}/reportable-months/refresh",
        ],
        "workflow_nodes": [
            {"id": "data_agent", "name": "Data Agent", "description": "Retrieves and processes price data"},
            {"id": "graph_designer", "name": "Graph Designer", "description": "Generates visualizations"},
            {"id": "news_retrieval", "name": "News Retrieval", "description": "Retrieves contextual news"},
            {"id": "event_mapper", "name": "Event Mapper", "description": "Extracts key events"},
            {"id": "trend_analyst", "name": "Trend Analyst", "description": "Analyzes market trends"},
            {"id": "module_orchestrator", "name": "Module Orchestrator", "description": "Runs optional modules"},
            {"id": "highlights_drafter", "name": "Highlights Drafter", "description": "Drafts highlights section"},
            {"id": "narrative_drafter", "name": "Narrative Drafter", "description": "Drafts narrative sections"},
            {"id": "red_team", "name": "Red Team QA", "description": "Quality assurance and fact-checking"},
        ],
        "available_modules": [
            {
                "id": "exchange_rate",
                "name": "Exchange Rate Analysis",
                "description": "Exchange rate analysis using DataBridges FX, with TradingEconomics as fallback when TE_API_KEY is configured",
            },
            {
                "id": "fuel_energy",
                "name": "Fuel & Energy",
                "description": "Transport fuel price analysis using DataBridges/PriceCache diesel and petrol-gasoline series",
            },
            {
                "id": "livestock_animal_products",
                "name": "Livestock & Animal Products",
                "description": "Livestock, meat, milk, and egg price analysis using DataBridges/PriceCache series",
            },
            {
                "id": "labour_market",
                "name": "Labour Market",
                "description": "Daily wage and staple purchasing-power analysis using DataBridges/PriceCache wage series",
            }
        ],
    }


def _market_monitor_dataset_status() -> LocalResponse:
    raise LocalHTTPException(
        404,
        "The processed Price Bulletin dataset upload/status path has been removed. Data is loaded from PriceCache.",
    )


def _market_monitor_dataset_upload(*, files: Any) -> LocalResponse:
    raise LocalHTTPException(
        404,
        "The processed Price Bulletin dataset upload path has been removed. Data is loaded from PriceCache.",
    )


def _market_monitor_countries() -> LocalResponse:
    cache_status = get_cache_status_snapshot()
    return _json_response(
        {
            "countries": get_market_monitor_supported_countries(),
            "cache_status": cache_status,
            "warnings": cache_status.get("warnings") or [],
            "operator_warnings": cache_status.get("operator_warnings") or [],
        }
    )


def _market_monitor_commodities(*, params: Dict[str, Any]) -> LocalResponse:
    country = _get_form_value(params, "country", None)
    if not country:
        return _json_response(
            {
                "commodities": [],
                "categories": {},
                "warning": "Select a country to load PriceCache commodity options.",
            }
        )

    country_normalized = normalize_country_name(str(country))
    commodity_list = get_available_commodities(country_normalized)
    categories = get_commodity_categories(commodity_list)
    return _json_response(
        {
            "country": country_normalized,
            "commodities": [{"name": c} for c in commodity_list],
            "categories": categories,
        }
    )


def _market_monitor_country_metadata(country: str) -> LocalResponse:
    try:
        return _json_response(get_market_monitor_country_metadata(country))
    except PriceCacheUnavailableError as exc:
        raise LocalHTTPException(503, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_country_reportable_months(country: str, *, params: Dict[str, Any]) -> LocalResponse:
    try:
        if not params:
            return _json_response(get_reportable_months(country))
        input_data = _validate_reportable_months_input(params)
        normalized_include_secondary, normalized_secondary_id = normalize_secondary_request(input_data)
        return _json_response(
            get_reportable_months(
                country,
                basket_version_id=input_data.basket_version_id,
                primary_basket_version_id=input_data.primary_basket_version_id,
                include_secondary_basket=normalized_include_secondary,
                secondary_basket_version_id=normalized_secondary_id,
                admin1_list=input_data.admin1_list,
            )
        )
    except LocalHTTPException:
        raise
    except SecondBasketFeatureDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except BasketVersionConflict as exc:
        raise LocalHTTPException(409, str(exc))
    except BasketScopeValidationError as exc:
        raise LocalHTTPException(400, str(exc))
    except PriceCacheUnavailableError as exc:
        raise LocalHTTPException(503, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_refresh_country_reportable_months(country: str, *, json_body: Any) -> LocalResponse:
    input_data = _validate_reportable_months_input(json_body if isinstance(json_body, dict) else {})
    try:
        normalized_include_secondary, normalized_secondary_id = normalize_secondary_request(input_data)
        if not any(
            [
                input_data.primary_basket_version_id,
                input_data.include_secondary_basket,
                input_data.secondary_basket_version_id,
                input_data.admin1_list,
            ]
        ):
            return _json_response(
                refresh_reportable_months_from_databridges(
                    country,
                    basket_version_id=input_data.basket_version_id,
                )
            )
        return _json_response(
            refresh_reportable_months_from_databridges(
                country,
                basket_version_id=input_data.basket_version_id,
                primary_basket_version_id=input_data.primary_basket_version_id,
                include_secondary_basket=normalized_include_secondary,
                secondary_basket_version_id=normalized_secondary_id,
                admin1_list=input_data.admin1_list,
            )
        )
    except SecondBasketFeatureDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except BasketVersionConflict as exc:
        raise LocalHTTPException(409, str(exc))
    except PriceCacheUnavailableError as exc:
        raise LocalHTTPException(503, str(exc))
    except BasketScopeValidationError as exc:
        raise LocalHTTPException(400, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_country_basket(country: str) -> LocalResponse:
    try:
        return _json_response(get_country_basket_response(country))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_country_baskets(country: str) -> LocalResponse:
    try:
        return _json_response(get_country_baskets_response(country))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_save_country_basket_role(
    country: str,
    *,
    role: BasketRole | str,
    json_body: Any,
) -> LocalResponse:
    input_data = _validate_basket_save_input(json_body)
    try:
        return _json_response(save_country_basket_role(country, role, input_data))
    except SecondBasketFeatureDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except BasketValidationError as exc:
        raise LocalHTTPException(400, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_country_basket_role_history(
    country: str,
    *,
    role: BasketRole | str,
    params: Dict[str, Any],
) -> LocalResponse:
    try:
        limit = int(_get_form_value(params, "limit", 20))
    except (TypeError, ValueError) as exc:
        raise LocalHTTPException(422, "limit must be an integer") from exc
    try:
        return _json_response(list_country_basket_role_history(country, role, limit=limit))
    except BasketValidationError as exc:
        raise LocalHTTPException(400, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_archive_country_secondary_basket(country: str) -> LocalResponse:
    try:
        return _json_response(archive_country_secondary_basket(country))
    except SecondBasketFeatureDisabled as exc:
        raise LocalHTTPException(exc.status_code, exc.to_dict())
    except BasketValidationError as exc:
        raise LocalHTTPException(400, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_save_country_basket(country: str, *, json_body: Any) -> LocalResponse:
    if not isinstance(json_body, dict):
        raise LocalHTTPException(400, "Invalid JSON body")
    try:
        return _json_response(save_country_basket(country, json_body))
    except BasketValidationError as exc:
        raise LocalHTTPException(400, str(exc))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _market_monitor_country_basket_history(country: str, *, params: Dict[str, Any]) -> LocalResponse:
    limit = _get_form_value(params, "limit", 20)
    try:
        return _json_response(list_country_basket_history(country, limit=int(limit)))
    except ValueError as exc:
        raise LocalHTTPException(404, str(exc))
    except Exception as exc:
        raise LocalHTTPException(500, str(exc))


def _get_food_basket_commodities(available: List[str]) -> List[str]:
    defaults: List[str] = []
    priority_patterns = [
        ("sorghum", "Cereals"),
        ("maize", "Cereals"),
        ("wheat", "Cereals"),
        ("rice", "Cereals"),
        ("beans", "Pulses"),
        ("lentil", "Pulses"),
        ("oil", "Oil"),
        ("salt", "Condiments"),
        ("sugar", "Sugar"),
    ]

    selected_categories = set()

    for pattern, category in priority_patterns:
        if category in selected_categories and category != "Cereals":
            continue
        for commodity in available:
            if pattern in commodity.lower() and commodity not in defaults:
                defaults.append(commodity)
                selected_categories.add(category)
                break

    return defaults[:6]


def dispatch_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Any = None,
    data: Optional[Dict[str, Any]] = None,
    files: Any = None,
) -> LocalResponse:
    parts, merged_params = _parse_path_and_params(path, params)
    method = (method or "GET").upper()

    if not parts:
        return _json_response(
            {
                "status": "ok",
                "services": [
                    {"id": "market-monitor", "name": "Market Monitor Generator", "endpoint": "/market-monitor/generate"},
                    {"id": "mfi-drafter", "name": "MFI Report Generator", "endpoint": "/mfi-drafter/generate"},
                    {"id": "seasonal-outlook", "name": "Seasonal Outlook Drafter", "endpoint": "/seasonal-outlook/info"},
                ],
            }
        )

    if parts == ["health"]:
        return _json_response({"status": "healthy"})

    service = parts[0]
    remainder = parts[1:]

    try:
        if service == "seasonal-outlook":
            from app.services.seasonal_outlook.api import handle
            reply = handle(method, remainder, json_body if json_body is not None else data, merged_params, _extract_file(files, 'file'))
            if reply.data is not None:
                return _json_response(reply.data, status_code=reply.status)
            headers = {'Content-Type': reply.mime}
            if reply.filename:
                headers['Content-Disposition'] = f'attachment; filename="{reply.filename}"'
            return LocalResponse(status_code=reply.status, headers=headers, content=reply.content)
        if service == "market-monitor":
            return _dispatch_market_monitor(
                method,
                remainder,
                json_body=json_body,
                data=data,
                files=files,
                params=merged_params,
            )
        if service == "mfi-drafter":
            return _dispatch_mfi_drafter(
                method,
                remainder,
                json_body=json_body,
                data=data,
                files=files,
                params=merged_params,
            )
    except LocalHTTPException as exc:
        return _error_response(exc.status_code, exc.detail)
    except Exception as exc:
        logger.exception("Local dispatcher error")
        return _error_response(500, str(exc))

    return _error_response(404, f"Unknown service: {service}")
