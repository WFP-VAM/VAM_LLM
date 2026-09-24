"""
MFI Drafter - Router
====================
FastAPI endpoints for the MFI Report Generator service.
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Body, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import json
import logging

from .graph import (
    reconcile_correction_history_for_failure,
    reconcile_generation_diagnostics_for_blocked_failure,
    reconcile_generation_diagnostics_for_llm_failure,
)
from .light_service import MOCK_DATA_REQUIRED, run_mfi_report_generation, runtime_status as light_runtime_status
from .errors import MFIGenerationBlockedError, MFIRunError
from .data_loader import load_mfi_from_csv, validate_csv_structure
from .compatibility import canonical_and_legacy_response_fields
from .context_status import not_attempted_context_status
from .features import (
    MFIAnalysisVersionDisabled,
    mfi_release_control,
    require_mfi_analysis_v2,
)
from .schemas import (
    GenerateMFIReportInput,
    GenerateMFIReportOutput,
    LightMFIReportOutput,
    MFIReportStatusOutput,
    MFI_DIMENSIONS,
)

from app.shared.async_runs import (
    create_run,
    get_run,
    get_run_artifact,
    set_run_completed,
    set_run_failed,
    update_run,
    update_run_progress,
)
from app.shared.live_outputs import (
    build_document_live_output,
    create_document_previews_with_artifacts,
)

from app.shared.docx_export import build_content_disposition, build_docx_bytes_from_report_blocks
from app.shared.report_blocks import resolve_mfi_report_blocks
from app.shared.llm_observability import LLMCallError, observability_config
from app.shared.llm import (
    LLMRuntimeConfigurationError,
    llm_runtime_status,
    require_llm_runtime_config,
)

logger = logging.getLogger(__name__)

router = APIRouter()

class ExportDocxOptions(BaseModel):
    filename: Optional[str] = None
    include_sources: bool = True
    include_visualizations: bool = True
    template: Optional[str] = None


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


def _analysis_run_metadata(state: Dict[str, Any]) -> Dict[str, Any]:
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


def _record_llm_failure_metadata(run_id: str, error: LLMCallError) -> None:
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


def _record_blocked_failure_metadata(
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


def _require_enabled_release_control():
    try:
        control = require_mfi_analysis_v2()
    except MFIAnalysisVersionDisabled as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    return control

def _build_mfi_output(
    *,
    result: Dict[str, Any],
    country: str,
    data_collection_start: str,
    data_collection_end: str,
) -> GenerateMFIReportOutput:
    if result.get("workflow_revision") == "mfi-light-v1":
        from .light_report import public_output
        return LightMFIReportOutput.model_validate(public_output(result))
    response_fields = canonical_and_legacy_response_fields(result)

    return GenerateMFIReportOutput(
        run_id=result.get("run_id", "unknown"),
        workflow_revision=(result.get("assessment_profile") or {}).get("workflow_revision"),
        country=country,
        data_collection_start=data_collection_start,
        data_collection_end=data_collection_end,
        analysis_schema_version=result.get("analysis_schema_version", "2.0"),
        methodology_version=result.get("methodology_version", "databridge-current"),
        score_authority=result.get("score_authority", "synthetic_mock"),
        release_control=result.get("release_control") or mfi_release_control(),
        generation_diagnostics=result.get("generation_diagnostics", {}),
        llm_diagnostics=result.get("llm_diagnostics") or {
            "service": "mfi-drafter",
            "run_id": result.get("run_id", "unknown"),
        },
        excluded_market_records=result.get("excluded_market_records", []),
        methodology_warnings=result.get("methodology_warnings", []),
        survey_metadata=result.get("survey_metadata", {}),
        national_mfi=response_fields["national_mfi"],
        risk_distribution=response_fields["risk_distribution"],
        markets_data=response_fields["markets_data"],
        dimension_scores=response_fields["dimension_scores"],
        mean_mfi_across_assessed_markets=response_fields[
            "mean_mfi_across_assessed_markets"
        ],
        assessment_profile=response_fields["assessment_profile"],
        narrative_schema_version=result.get("narrative_schema_version", "2.0"),
        market_score_distribution=response_fields["market_score_distribution"],
        context_status=(
            result.get("context_status")
            or not_attempted_context_status().model_dump()
        ),
        context_evidence=result.get("context_evidence", []),
        dimension_narratives=result.get("dimension_narratives", {}),
        market_narratives=result.get("market_narratives", {}),
        executive_summary_narrative=result.get(
            "executive_summary_narrative", {}
        ),
        claim_validation=result.get("claim_validation", {}),
        qa_review=result.get("qa_review", {}),
        executive_summary=response_fields["executive_summary"],
        dimension_findings=response_fields["dimension_findings"],
        market_recommendations=response_fields["market_recommendations"],
        country_context=response_fields["country_context"],
        document_references=result.get("document_references", []),
        report_blocks=resolve_mfi_report_blocks(
            result,
            country=country,
            data_collection_start=data_collection_start,
            data_collection_end=data_collection_end,
        ),
        visualizations=result.get("visualizations", {}),
        warnings=result.get("warnings", []),
        llm_calls=result.get("llm_calls", 0),
        correction_attempts=result.get("correction_attempts", 0),
        success=True,
    )


def _run_mfi_from_structured_data(
    csv_data: Dict[str, Any],
    *,
    release_control,
) -> GenerateMFIReportOutput:
    country = csv_data["country"]
    data_collection_start = csv_data["data_collection_start"]
    data_collection_end = csv_data["data_collection_end"]
    markets = csv_data["markets"]
    result = run_mfi_report_generation(
        country=country,
        data_collection_start=data_collection_start,
        data_collection_end=data_collection_end,
        markets=markets,
        csv_data=csv_data,
        release_control=release_control,
    )
    return _build_mfi_output(
        result=result,
        country=country,
        data_collection_start=data_collection_start,
        data_collection_end=data_collection_end,
    )

@router.post("/generate", response_model=GenerateMFIReportOutput | LightMFIReportOutput)
async def generate_mfi_report(input_data: GenerateMFIReportInput):
    """
    Generates a full MFI report.

    The process includes:
    1. MFI Data Agent: Retrieves/generates MFI data for markets
    2. Context Retrieval: Retrieves contextual news
    3. Context Extractor: Extracts context with the LLM
    4. Graph Designer: Generates visualizations (radar, heatmap, etc.)
    5. Dimension Drafter: Drafts findings for each dimension
    6. Market Recommendations Drafter: Drafts recommendations by market
    7. Executive Summary Drafter: Drafts the executive summary
    8. Red Team: Quality assurance with possible correction loop

    Returns:
        GenerateMFIReportOutput with all report sections
    """
    release_control = _require_enabled_release_control()
    if not input_data.use_mock_data:
        raise HTTPException(status_code=400, detail=MOCK_DATA_REQUIRED)
    try:
        logger.info(f"Starting MFI report generation for {input_data.country}")

        result = run_mfi_report_generation(
            country=input_data.country,
            data_collection_start=input_data.data_collection_start,
            data_collection_end=input_data.data_collection_end,
            markets=input_data.markets,
            release_control=release_control,
            use_mock_data=True,
        )
        
        output = _build_mfi_output(
            result=result,
            country=input_data.country,
            data_collection_start=input_data.data_collection_start,
            data_collection_end=input_data.data_collection_end,
        )
        
        logger.info(f"MFI report generation completed: {output.run_id}")
        
        return output
        
    except LLMCallError as e:
        logger.error("MFI report generation stopped: %s", e)
        raise HTTPException(status_code=502, detail=e.to_public_dict())
    except MFIGenerationBlockedError as e:
        logger.error("MFI report generation blocked: %s", e)
        raise HTTPException(status_code=e.status_code, detail=e.to_public_dict())
    except MFIRunError as e:
        logger.error("MFI report generation stopped: %s", e)
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        logger.error(f"MFI report generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate-from-csv", response_model=GenerateMFIReportOutput | LightMFIReportOutput)
async def generate_mfi_report_from_csv(
    file: UploadFile = File(..., description="Processed MFI CSV file"),
    country_override: Optional[str] = Form(None, description="Override country name"),
    data_collection_start_override: Optional[str] = Form(None, description="Override start date"),
    data_collection_end_override: Optional[str] = Form(None, description="Override end date"),
):
    """Generates a full MFI report from an uploaded CSV file."""
    release_control = _require_enabled_release_control()
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    try:
        content = await file.read()

        logger.info(f"Loading CSV file: {filename}")
        csv_data = load_mfi_from_csv(
            file_content=content,
            country_override=country_override,
            start_date_override=data_collection_start_override,
            end_date_override=data_collection_end_override,
        )
        logger.info(
            "Starting MFI report generation from CSV for %s (%s markets)",
            csv_data["country"],
            len(csv_data["markets"]),
        )
        output = _run_mfi_from_structured_data(
            csv_data,
            release_control=release_control,
        )
        logger.info(f"MFI report generation from CSV completed: {output.run_id}")
        return output
    except LLMCallError as e:
        logger.error("MFI report generation from CSV stopped: %s", e)
        raise HTTPException(status_code=502, detail=e.to_public_dict())
    except MFIGenerationBlockedError as e:
        logger.error("MFI report generation from CSV blocked: %s", e)
        raise HTTPException(status_code=e.status_code, detail=e.to_public_dict())
    except MFIRunError as e:
        logger.error("MFI report generation from CSV stopped: %s", e)
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except ValueError as e:
        logger.error(f"CSV validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"MFI report generation from CSV failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/validate-csv")
async def validate_mfi_csv(
    file: UploadFile = File(..., description="CSV file to validate"),
):
    """Validates a CSV file structure before processing."""
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    try:
        content = await file.read()
        return validate_csv_structure(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CSV validation failed: {str(e)}")


@router.post("/generate-from-csv-async")
async def generate_mfi_report_from_csv_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="Processed MFI CSV file"),
    country_override: Optional[str] = Form(None),
    data_collection_start_override: Optional[str] = Form(None),
    data_collection_end_override: Optional[str] = Form(None),
):
    """Starts report generation from CSV in the background."""
    import uuid as uuid_module
    release_control = _require_enabled_release_control()

    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="File must be a CSV")

    content = await file.read()

    try:
        csv_data = load_mfi_from_csv(
            file_content=content,
            country_override=country_override,
            start_date_override=data_collection_start_override,
            end_date_override=data_collection_end_override,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    run_id = f"mfi_{uuid_module.uuid4().hex[:8]}"
    create_run(run_id)
    from app.services.mfi_drafter.light_service import validate_submission
    try:
        validate_submission(csv_data)
    except MFIRunError as exc:
        set_run_failed(run_id, error=str(exc))
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
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

    def run_in_background():
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            def on_step(node_name: str, _state: dict):
                progress = (_state.get("generation_diagnostics") or {}).get("progress_pct", progress_map.get(node_name))
                if progress is not None:
                    update_run_progress(run_id, current_node=node_name, progress_pct=progress)
                else:
                    update_run(run_id, current_node=node_name)

                meta_update: Dict[str, Any] = {}
                meta_update.update(_analysis_run_metadata(_state))
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
            )

            update_run(run_id, warnings=result.get("warnings", []))
            set_run_completed(run_id, result=result)
        except Exception as e:
            import traceback

            if isinstance(e, LLMCallError):
                _record_llm_failure_metadata(run_id, e)
            elif isinstance(e, MFIGenerationBlockedError):
                _record_blocked_failure_metadata(run_id, e)
            public_failure = isinstance(e, (LLMCallError, MFIGenerationBlockedError))
            tb_str = None if public_failure else traceback.format_exc()
            current_node = (
                e.node
                if public_failure
                else (get_run(run_id).current_node if get_run(run_id) is not None else None)
            )
            error = json.dumps(e.to_public_dict(), sort_keys=True) if public_failure else str(e)
            set_run_failed(run_id, error=error, traceback=tb_str, current_node=current_node)

    background_tasks.add_task(run_in_background)

    return {
        "run_id": run_id,
        "status": "pending",
        "preview": {
            "country": csv_data["country"],
            "markets_count": len(csv_data["markets"]),
            "collection_period": csv_data["survey_metadata"]["collection_period"],
        },
    }


@router.post("/generate-async")
async def generate_mfi_report_async(
    input_data: GenerateMFIReportInput,
    background_tasks: BackgroundTasks
):
    """
    Starts report generation in the background.

    Returns:
        run_id for polling status
    """
    import uuid
    release_control = _require_enabled_release_control()
    if not input_data.use_mock_data:
        raise HTTPException(status_code=400, detail=MOCK_DATA_REQUIRED)
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
    
    def run_in_background():
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            def on_step(node_name: str, _state: dict):
                progress = (_state.get("generation_diagnostics") or {}).get("progress_pct", progress_map.get(node_name))
                if progress is not None:
                    update_run_progress(run_id, current_node=node_name, progress_pct=progress)
                else:
                    update_run(run_id, current_node=node_name)

                meta_update = _analysis_run_metadata(_state)
                context_counts = _state.get("context_counts")
                if isinstance(context_counts, dict):
                    meta_update["context_counts"] = context_counts
                retriever_traces = _state.get("retriever_traces")
                if isinstance(retriever_traces, list):
                    meta_update["retriever_traces"] = retriever_traces
                if meta_update:
                    update_run(run_id, metadata=meta_update)
            
            result = run_mfi_report_generation(
                country=input_data.country,
                data_collection_start=input_data.data_collection_start,
                data_collection_end=input_data.data_collection_end,
                markets=input_data.markets,
                on_step=on_step,
                release_control=release_control,
                run_id=run_id,
                llm_trace_sink=on_llm_trace,
                use_mock_data=True,
            )
            
            update_run(run_id, warnings=result.get("warnings", []))
            set_run_completed(run_id, result=result)
            
        except Exception as e:
            import traceback

            if isinstance(e, LLMCallError):
                _record_llm_failure_metadata(run_id, e)
            elif isinstance(e, MFIGenerationBlockedError):
                _record_blocked_failure_metadata(run_id, e)
            public_failure = isinstance(e, (LLMCallError, MFIGenerationBlockedError))
            tb_str = None if public_failure else traceback.format_exc()
            current_node = (
                e.node
                if public_failure
                else (get_run(run_id).current_node if get_run(run_id) is not None else None)
            )
            error = json.dumps(e.to_public_dict(), sort_keys=True) if public_failure else str(e)
            set_run_failed(run_id, error=error, traceback=tb_str, current_node=current_node)
    
    background_tasks.add_task(run_in_background)
    
    return {"run_id": run_id, "status": "pending"}


@router.get("/status/{run_id}", response_model=MFIReportStatusOutput)
async def get_report_status(run_id: str):
    """Checks the status of an in-progress report."""
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run ID not found: {run_id}")

    return MFIReportStatusOutput(
        run_id=run_id,
        status=run.status,
        current_node=run.current_node,
        progress_pct=run.progress_pct,
        warnings=run.warnings,
        metadata=getattr(run, "metadata", {}) or {},
        error=run.error,
        traceback=run.traceback,
    )


@router.get("/result/{run_id}", response_model=GenerateMFIReportOutput | LightMFIReportOutput)
async def get_report_result(run_id: str):
    """Retrieves the result of a completed report."""
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run ID not found: {run_id}")

    if run.status != "completed":
        raise HTTPException(
            status_code=400, 
            detail=f"Report not completed. Current status: {run.status}"
        )

    result = run.result or {}
    return _build_mfi_output(
        result={**result, "run_id": run_id},
        country=result.get("country", "Unknown"),
        data_collection_start=result.get("data_collection_start", "Unknown"),
        data_collection_end=result.get("data_collection_end", "Unknown"),
    )


@router.get("/artifacts/{run_id}/{artifact_id}")
async def get_report_artifact(run_id: str, artifact_id: str):
    artifact = get_run_artifact(run_id, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail=f"Artifact not found: {artifact_id}")

    return Response(
        content=artifact.content,
        media_type=artifact.mime_type,
        headers={"Content-Disposition": build_content_disposition(artifact.file_name)},
    )


@router.post("/export-docx/{run_id}")
async def export_mfi_docx(
    run_id: str,
    options: ExportDocxOptions = Body(default_factory=ExportDocxOptions),
):
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run ID not found: {run_id}")

    if run.status != "completed":
        raise HTTPException(status_code=409, detail=f"Run not completed. Current status: {run.status}")

    result = run.result or {}
    try:
        report_blocks = resolve_mfi_report_blocks(result)
        docx_bytes = build_docx_bytes_from_report_blocks(
            report_blocks,
            visualizations=result.get("visualizations", {}),
            include_sources=options.include_sources,
            include_visualizations=options.include_visualizations,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DOCX generation failed: {str(e)}")

    filename = options.filename or f"mfi-drafter-{run_id}.docx"
    headers = {"Content-Disposition": build_content_disposition(filename)}
    logger.info(
        "MFI Drafter DOCX export completed",
        extra={
            "mfi_event": "docx_export_completed",
            "mfi_run_id": run_id,
            "mfi_docx_bytes": len(docx_bytes),
        },
    )
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


@router.get("/info")
def get_service_info():
    """Returns service metadata for the frontend."""
    release_control = mfi_release_control()
    trace_config = observability_config()
    return {
        "id": "mfi-drafter",
        "name": "MFI Report Generator",
        "description": "Generates full Market Functionality Index (MFI) reports. "
                       "Analyzes 9 market functionality dimensions and generates "
                       "visualizations, an executive summary, and recommendations.",
        "version": "2.0.0",
        "release_control": release_control.model_dump(),
        "generation_enabled": release_control.enabled,
        "llm_observability": trace_config.model_dump(),
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
            "success": "True if generation is completed"
        },
        "workflow_nodes": [
            {"id": "mfi_data_agent", "name": "MFI Data Agent", "description": "Retrieves/generates MFI data"},
            {"id": "mfi_analysis", "name": "MFI Analysis", "description": "Builds the deterministic assessment profile"},
            {"id": "context_retrieval", "name": "Context Retrieval", "description": "Retrieves contextual news"},
            {"id": "context_extractor", "name": "Context Extractor", "description": "Extracts context with the LLM"},
            {"id": "mfi_graph_designer", "name": "Graph Designer", "description": "Generates visualizations"},
            {"id": "dimension_drafter", "name": "Dimension Drafter", "description": "Drafts findings per dimension"},
            {"id": "market_recommendations_drafter", "name": "Market Recommendations", "description": "Drafts recommendations by market"},
            {"id": "executive_summary_drafter", "name": "Executive Summary", "description": "Drafts executive summary"},
            {"id": "deterministic_claim_validator", "name": "Claim Validator", "description": "Validates every claim against the closed catalog"},
            {"id": "semantic_review", "name": "Semantic Review", "description": "Runs the three bounded narrative reviews"},
            {"id": "consolidated_correction", "name": "Consolidated Correction", "description": "Repairs all material fields in one call"},
            {"id": "post_correction_validator", "name": "Post-correction Validation", "description": "Revalidates numbers and citations"},
            {"id": "corrected_claim_verification", "name": "Correction Verification", "description": "Reviews only corrected claims"},
            {"id": "finalize_qa", "name": "Finalize QA", "description": "Finalizes warnings and claim status"},
            {"id": "finalize_delivery", "name": "Validate Delivery", "description": "Validates and stores reader-facing report blocks"},
        ],
        "mfi_dimensions": MFI_DIMENSIONS
    }


@router.get("/health")
def health_check():
    """Health check endpoint."""
    release_control = mfi_release_control()
    trace_config = observability_config()
    return {
        "status": "healthy",
        "service": "mfi-drafter",
        "generation_enabled": release_control.enabled,
        "release_control": release_control.model_dump(),
        "llm_observability": trace_config.model_dump(),
        "llm_runtime": light_runtime_status(),
    }


@router.get("/dimensions")
def get_mfi_dimensions():
    """Returns the 9 MFI dimensions with descriptions."""
    from .methodology import DIMENSION_DESCRIPTIONS

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


@router.get("/sample-markets")
def get_sample_markets():
    """Returns sample markets for testing."""
    return {
        "Ghana": {
            "markets": [
                "Gushegu", "Karaga", "Nanton", "Sang", "Tamale Aboabo", "Yendi",
                "Fumbisi", "Bussie", "Gwollu", "Nyoli", "Tangasie", "Tumu"
            ]
        },
        "Sudan": {
            "markets": [
                "Omdurman", "Khartoum Central", "El Fasher", "Nyala",
                "Kassala City", "Gedaref", "Port Sudan"
            ]
        },
        "Yemen": {
            "markets": [
                "Sana'a Central", "Aden Port", "Taiz City",
                "Hodeidah", "Mukalla", "Ibb"
            ]
        }
    }
