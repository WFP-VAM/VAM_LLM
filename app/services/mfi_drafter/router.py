"""
MFI Drafter - Router
====================
FastAPI endpoints for the MFI Report Generator service.
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Body, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional, Any, Dict, List
import logging

from .light_service import (
    run_mfi_report_generation,
    runtime_status as light_runtime_status,
    service_info,
    validate_submission,
)
from .errors import MFIRunError
from .data_loader import load_mfi_from_csv, validate_csv_structure
from .features import (
    MFIAnalysisVersionDisabled,
    mfi_release_control,
    require_mfi_analysis_v2,
)
from .schemas import (
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
from app.shared.llm_observability import observability_config

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
        "narrative_schema_version": state.get("narrative_schema_version"),
    })
    return metadata


def _require_enabled_release_control():
    try:
        control = require_mfi_analysis_v2()
    except MFIAnalysisVersionDisabled as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    return control


def _build_mfi_output(result: Dict[str, Any]) -> LightMFIReportOutput:
    from .light_report import public_output
    if result.get("workflow_revision") != "mfi-light-v1":
        raise HTTPException(status_code=410, detail="Reports produced by the previous MFI workflow are no longer supported")
    return LightMFIReportOutput.model_validate(public_output(result))


def _run_mfi_from_structured_data(
    csv_data: Dict[str, Any],
    *,
    release_control,
) -> LightMFIReportOutput:
    result = run_mfi_report_generation(
        country=csv_data["country"],
        data_collection_start=csv_data["data_collection_start"],
        data_collection_end=csv_data["data_collection_end"],
        markets=csv_data["markets"],
        csv_data=csv_data,
        release_control=release_control,
    )
    return _build_mfi_output(result)


@router.post("/generate-from-csv", response_model=LightMFIReportOutput)
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
    try:
        validate_submission(csv_data)
    except MFIRunError as exc:
        set_run_failed(run_id, error=str(exc))
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    update_run(
        run_id,
        metadata={"release_control": release_control.model_dump(), "workflow_revision": "mfi-light-v1"},
    )

    def run_in_background():
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            def on_step(node_name: str, _state: dict):
                progress = (_state.get("generation_diagnostics") or {}).get("progress_pct")
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

            current = get_run(run_id)
            set_run_failed(run_id, error=str(e), traceback=traceback.format_exc(),
                           current_node=current.current_node if current is not None else None)

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


@router.get("/result/{run_id}", response_model=LightMFIReportOutput)
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

    return _build_mfi_output({**(run.result or {}), "run_id": run_id})


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
    return service_info()


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
