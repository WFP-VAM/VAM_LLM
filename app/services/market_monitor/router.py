"""
Market Monitor - Router
=======================
FastAPI endpoints for the Market Monitor service.
"""
from fastapi import APIRouter, HTTPException, BackgroundTasks, Body, Query
from fastapi.responses import Response
from pydantic import BaseModel, ValidationError
from typing import Optional, List, Any, Dict
from dataclasses import is_dataclass, asdict
from datetime import date, datetime
import json
import logging
import threading
import traceback

from .graph import run_report_generation, AVAILABLE_MODULES, normalize_qa_review
from .price_backfill import PriceDataGateError
from .basket_calculation import BasketScopeValidationError
from .food_basket import (
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
from .features import (
    SecondBasketFeatureDisabled,
    market_monitor_second_basket_enabled,
    normalize_secondary_request,
    second_basket_feature_metadata,
)
from .schemas import (
    BasketArchiveOutput,
    BasketConfigurationOutput,
    BasketHistoryOutput,
    GenerateReportInput,
    GenerateReportOutput,
    ReportableMonthsInput,
    ReportStatusOutput
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
    build_databridges_live_output,
    build_document_live_output,
    create_databridges_artifacts,
    create_document_previews_with_artifacts,
)

from app.shared.docx_export import build_content_disposition, build_docx_bytes_from_report_blocks
from app.shared.report_blocks import build_market_monitor_report_blocks
from app.shared.llm_observability import LLMCallError, observability_config
from app.shared.llm import llm_runtime_status
from .i18n import resolve_report_language, t

logger = logging.getLogger(__name__)

router = APIRouter()

_PRICE_CACHE_REPOSITORY: Any = None
_PRICE_CACHE_REPOSITORY_LOCK = threading.Lock()


class ExportDocxOptions(BaseModel):
    filename: Optional[str] = None
    include_sources: bool = True
    include_visualizations: bool = True
    template: Optional[str] = None


def _get_price_cache_repository():
    global _PRICE_CACHE_REPOSITORY
    if _PRICE_CACHE_REPOSITORY is not None:
        return _PRICE_CACHE_REPOSITORY
    from app.services.price_cache.config import load_price_cache_config
    from app.services.price_cache.migrations import apply_migrations
    from app.services.price_cache.sql_repository import (
        SqlPriceCacheRepository,
        create_price_cache_engine,
    )

    with _PRICE_CACHE_REPOSITORY_LOCK:
        if _PRICE_CACHE_REPOSITORY is None:
            config = load_price_cache_config()
            engine = create_price_cache_engine(config)
            apply_migrations(engine, config.backend)
            _PRICE_CACHE_REPOSITORY = SqlPriceCacheRepository(engine)
    return _PRICE_CACHE_REPOSITORY


def _cache_json(value: Any) -> Any:
    if is_dataclass(value):
        return _cache_json(asdict(value))
    if isinstance(value, dict):
        return {key: _cache_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_cache_json(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


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


@router.post("/generate", response_model=GenerateReportOutput)
async def generate_market_monitor(input_data: GenerateReportInput):
    """
    Generates a full Market Monitor report.

    The process includes:
    1. Data Agent: Retrieves/generates price data (mock or API)
    2. Graph Designer: Creates visualizations
    3. News Retrieval: Retrieves contextual news
    4. Event Mapper: Extracts key events
    5. Trend Analyst: Analyzes market trends
    6. Module Orchestrator: Runs optional modules (e.g., exchange rate)
    7. Highlights Drafter: Drafts the highlights section
    8. Narrative Drafter: Drafts narrative sections
    9. Red Team: Quality assurance with possible correction loop

    Returns:
        GenerateReportOutput with all report sections
    """
    try:
        logger.info(f"Starting report generation for {input_data.country} - {input_data.time_period}")
        include_secondary_basket, secondary_basket_version_id = normalize_secondary_request(input_data)

        admin1_list = input_data.admin1_list
        if not admin1_list and input_data.use_mock_data:
            admin1_list = [
                f"{input_data.country} North",
                f"{input_data.country} South",
                f"{input_data.country} Central"
            ]

        basket_selection = None
        if not input_data.use_mock_data:
            basket_selection = resolve_baskets_for_report(
                input_data.country,
                primary_basket_version_id=input_data.effective_primary_basket_version_id,
                include_secondary_basket=include_secondary_basket,
                secondary_basket_version_id=secondary_basket_version_id,
            )

        # Run generation
        result = run_report_generation(
            country=input_data.country,
            time_period=input_data.time_period,
            commodity_list=input_data.commodity_list,
            admin1_list=admin1_list,
            currency_code=input_data.currency_code,
            enabled_modules=input_data.enabled_modules,
            basket_version_id=(
                basket_selection.primary_basket_version_id
                if basket_selection is not None
                else input_data.effective_primary_basket_version_id
            ),
            basket_selection=basket_selection,
            previous_report_text=input_data.previous_report_text,
            use_mock_data=input_data.use_mock_data,
            language=input_data.language,
        )

        result = attach_basket_selection_to_result(result, basket_selection)

        # Build output
        output = GenerateReportOutput(
            run_id=result.get("run_id", "unknown"),
            country=input_data.country,
            time_period=input_data.time_period,
            language=result.get("language", "en"),
            locale=result.get("locale", "en_US"),
            language_source=result.get("language_source", "default"),
            report_sections=result.get("report_draft_sections", {}),
            report_blocks=build_market_monitor_report_blocks(
                {**(result or {}), "country": input_data.country, "time_period": input_data.time_period}
            ),
            visualizations=result.get("visualizations", {}),
            data_statistics=result.get("data_statistics", {}),
            trend_analysis=result.get("trend_analysis"),
            events=result.get("events", []),
            module_sections=result.get("module_sections", {}),
            document_references=result.get("document_references", []),
            news_counts=result.get("news_counts", {}),
            cache_metadata=result.get("cache_metadata", {}),
            food_basket=result.get("food_basket", {}),
            food_baskets=result.get("food_baskets", {}),
            basket_statistics=result.get("basket_statistics", {}),
            basket_series_national=result.get("basket_series_national", []),
            basket_series_regional=result.get("basket_series_regional", []),
            secondary_basket_included=bool(result.get("secondary_basket_included", False)),
            qa_review=normalize_qa_review(result),
            fuel_energy_data=result.get("fuel_energy_data"),
            livestock_animal_products_data=result.get("livestock_animal_products_data"),
            labour_market_data=result.get("labour_market_data"),
            warnings=result.get("warnings", []),
            llm_calls=result.get("llm_calls", 0),
            llm_diagnostics=result.get("llm_diagnostics") or {
                "service": "market-monitor",
                "run_id": result.get("run_id", "unknown"),
            },
            success=True
        )

        logger.info(f"Report generation completed: {output.run_id}")

        return output

    except PriceDataGateError as e:
        raise HTTPException(status_code=e.status_code, detail=e.to_dict())
    except SecondBasketFeatureDisabled as e:
        raise HTTPException(status_code=e.status_code, detail=e.to_dict())
    except (BasketNotConfigured, BasketVersionConflict) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (BasketValidationError, BasketScopeValidationError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LLMCallError as e:
        logger.error("Report generation stopped: %s", e)
        raise HTTPException(status_code=502, detail=e.to_public_dict())
    except Exception as e:
        logger.error(f"Report generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/generate-async")
async def generate_market_monitor_async(
    input_data: GenerateReportInput,
    background_tasks: BackgroundTasks
):
    """
    Starts report generation in the background.
    Useful for reports that take a long time.

    Returns:
        run_id for polling status
    """
    import uuid
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
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except (BasketNotConfigured, BasketVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BasketValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    language_info = resolve_report_language(input_data.country, input_data.language)
    language = language_info["language"]
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    create_run(run_id)
    initial_metadata = {**language_info, "feature_flags": submission_feature_flags}
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

    def run_in_background():
        try:
            update_run(run_id, status="running", error=None, traceback=None)

            def on_llm_trace(diagnostics: Dict[str, Any]) -> None:
                update_run(run_id, metadata={"llm_diagnostics": diagnostics})

            run_basket_selection = basket_selection
            if not input_data.use_mock_data and basket_selection is not None:
                # Revalidate that both immutable IDs are still current, but
                # calculate from the snapshots captured at submission.
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
                admin1_list = [
                    f"{input_data.country} North",
                    f"{input_data.country} South"
                ]

            def on_step(node_name: str, _state: dict):
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
                            title=t(language, "live.price_data.title"),
                            summary=(
                                t(
                                    language,
                                    "live.price_data.summary",
                                    rows=len(rows),
                                    country=input_data.country,
                                    period=input_data.time_period,
                                )
                            ),
                            rows=rows,
                            download_artifacts=artifacts,
                        )
                    elif input_data.use_mock_data:
                        section_updates["databridges"] = build_databridges_live_output(
                            title=t(language, "live.price_data.title"),
                            summary=t(language, "live.price_data.mock"),
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
                            title=t(language, "live.seerist.title"),
                            summary=(
                                t(language, "live.docs.unavailable", source="Seerist", error=seerist_error)
                                if seerist_error
                                else t(language, "live.docs.summary", count=len(seerist_docs), source="Seerist")
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
                            title=t(language, "live.reliefweb.title"),
                            summary=(
                                t(language, "live.docs.unavailable", source="ReliefWeb", error=reliefweb_error)
                                if reliefweb_error
                                else t(language, "live.docs.summary", count=len(reliefweb_docs), source="ReliefWeb")
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
                basket_version_id=(
                    run_basket_selection.primary_basket_version_id
                    if run_basket_selection is not None
                    else input_data.effective_primary_basket_version_id
                ),
                basket_selection=run_basket_selection,
                previous_report_text=input_data.previous_report_text,
                use_mock_data=input_data.use_mock_data,
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

        except Exception as e:
            tb_str = None if isinstance(e, LLMCallError) else traceback.format_exc()
            if isinstance(e, LLMCallError):
                logger.error("Report generation stopped for %s: %s", run_id, e)
            else:
                logger.exception(f"Report generation failed for {run_id}: {e}")

            current_node = (
                e.node
                if isinstance(e, LLMCallError)
                else (get_run(run_id).current_node if get_run(run_id) is not None else None)
            )
            if isinstance(e, PriceDataGateError):
                update_run(run_id, metadata={"price_gap_report": e.gap_report.to_dict()})
            error = json.dumps(e.to_public_dict(), sort_keys=True) if isinstance(e, LLMCallError) else str(e)
            set_run_failed(run_id, error=error, traceback=tb_str, current_node=current_node)

    background_tasks.add_task(run_in_background)

    return {"run_id": run_id, "status": "pending"}


@router.get("/data-availability")
def check_data_availability_endpoint(
    country: str,
    time_period: str = "2025-01",
    commodities: str = "Sugar,Wheat flour"
):
    '''
    Check what price data is available for a given country and period.

    Useful for:
    - Validating inputs before running a report
    - Showing users what data is available
    - Debugging data loading issues
    '''
    from .data_loader import check_data_availability

    commodity_list = [c.strip() for c in commodities.split(",")]

    availability = check_data_availability(
        country=country,
        time_period=time_period,
        commodities=commodity_list
    )

    return availability


@router.get("/cache/status")
def get_price_cache_status():
    try:
        from .data_loader import get_cache_status_snapshot

        return get_cache_status_snapshot()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/cache/refreshes")
def list_price_cache_refreshes(limit: int = 20):
    try:
        repository = _get_price_cache_repository()
        safe_limit = min(max(int(limit), 1), 100)
        return {"refreshes": _cache_json(repository.list_cache_refreshes(limit=safe_limit))}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/cache/refreshes/{cache_version_id}")
def get_price_cache_refresh(cache_version_id: str):
    try:
        repository = _get_price_cache_repository()
        refresh = repository.get_cache_refresh(cache_version_id)
        if refresh is None:
            raise HTTPException(status_code=404, detail=f"Cache refresh not found: {cache_version_id}")
        return _cache_json(refresh)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/status/{run_id}", response_model=ReportStatusOutput)
async def get_report_status(run_id: str):
    """
    Checks the status of an in-progress report.
    """
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run ID not found: {run_id}")

    return ReportStatusOutput(
        run_id=run_id,
        status=run.status,
        current_node=run.current_node,
        progress_pct=run.progress_pct,
        warnings=run.warnings,
        metadata=getattr(run, "metadata", {}) or {},
        error=run.error,
        traceback=run.traceback,
    )


@router.get("/result/{run_id}", response_model=GenerateReportOutput)
async def get_report_result(run_id: str):
    """
    Retrieves the result of a completed report.
    """
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run ID not found: {run_id}")

    if run.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Report not completed. Current status: {run.status}"
        )

    result = attach_basket_selection_to_result(run.result or {}, None)

    return GenerateReportOutput(
        run_id=run_id,
        country=result.get("country", "Unknown"),
        time_period=result.get("time_period", "Unknown"),
        language=result.get("language", "en"),
        locale=result.get("locale", "en_US"),
        language_source=result.get("language_source", "default"),
        report_sections=result.get("report_draft_sections", {}),
        report_blocks=build_market_monitor_report_blocks(result),
        visualizations=result.get("visualizations", {}),
        data_statistics=result.get("data_statistics", {}),
        trend_analysis=result.get("trend_analysis"),
        events=result.get("events", []),
        module_sections=result.get("module_sections", {}),
        document_references=result.get("document_references", []),
        news_counts=result.get("news_counts", {}),
        cache_metadata=result.get("cache_metadata", {}),
        food_basket=result.get("food_basket", {}),
        food_baskets=result.get("food_baskets", {}),
        basket_statistics=result.get("basket_statistics", {}),
        basket_series_national=result.get("basket_series_national", []),
        basket_series_regional=result.get("basket_series_regional", []),
        secondary_basket_included=bool(result.get("secondary_basket_included", False)),
        qa_review=normalize_qa_review(result),
        fuel_energy_data=result.get("fuel_energy_data"),
        livestock_animal_products_data=result.get("livestock_animal_products_data"),
        labour_market_data=result.get("labour_market_data"),
        warnings=result.get("warnings", []) or run.warnings,
        llm_calls=result.get("llm_calls", 0),
        llm_diagnostics=result.get("llm_diagnostics") or {
            "service": "market-monitor",
            "run_id": run_id,
        },
        success=True
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
async def export_market_monitor_docx(
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
        report_blocks = build_market_monitor_report_blocks(result)
        docx_bytes = build_docx_bytes_from_report_blocks(
            report_blocks,
            visualizations=result.get("visualizations", {}),
            include_sources=options.include_sources,
            include_visualizations=options.include_visualizations,
            language=result.get("language", "en"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DOCX generation failed: {str(e)}")

    filename = options.filename or f"market-monitor-{run_id}.docx"
    headers = {"Content-Disposition": build_content_disposition(filename)}
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


@router.get("/info")
def get_service_info():
    """
    Returns service metadata for the frontend.
    """
    second_basket_enabled = market_monitor_second_basket_enabled()
    trace_config = observability_config()
    return {
        "id": "market-monitor",
        "name": "Market Monitor Generator",
        "description": "Generates full Market Monitor reports with price analysis, "
                       "market trend analysis, visualizations, and narrative sections. "
                       "Includes optional modules such as exchange rate analysis.",
        "version": "1.0.0",
        "llm_observability": trace_config.model_dump(),
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
                "description": "Country name (e.g., 'Sudan', 'Yemen', 'Myanmar')"
            },
            {
                "name": "time_period",
                "type": "string",
                "required": True,
                "label": "Time Period",
                "description": "Period in YYYY-MM format (e.g., '2025-01')"
            },
            {
                "name": "language",
                "type": "string",
                "required": False,
                "label": "Language",
                "description": "Report language: auto country default, English, French, or Spanish",
                "default": "auto",
                "options": ["auto", "en", "fr", "es"]
            },
            {
                "name": "commodity_list",
                "type": "array",
                "required": False,
                "label": "Additional commodities",
                "description": "Optional commodities to analyze in addition to the active country food basket. Basket commodities are always included.",
                "default": [],
                "note": "Query /countries/{country}/baskets for active baskets and /countries/{country}/metadata for available additional commodities."
            },
            {
                "name": "basket_version_id",
                "type": "string",
                "required": False,
                "label": "Basket Version ID",
                "description": "Deprecated alias for primary_basket_version_id."
            },
            {
                "name": "primary_basket_version_id",
                "type": "string",
                "required": False,
                "label": "Primary Basket Version ID",
                "description": "Optional active primary basket version guard. Stale versions return a conflict."
            },
            {
                "name": "include_secondary_basket",
                "type": "boolean",
                "required": False,
                "label": "Include Secondary Basket",
                "description": "Include the active secondary basket when one exists.",
                "default": True
            },
            {
                "name": "secondary_basket_version_id",
                "type": "string",
                "required": False,
                "label": "Secondary Basket Version ID",
                "description": "Optional active secondary basket version guard. Ignored when inclusion is false."
            },
            {
                "name": "admin1_list",
                "type": "array",
                "required": False,
                "label": "Regions (Admin1)",
                "description": "List of regions to include"
            },
            {
                "name": "currency_code",
                "type": "string",
                "required": False,
                "label": "Currency Code",
                "description": "ISO 4217 currency code (e.g., 'SDG', 'YER')",
                "default": "USD"
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
                "options": list(AVAILABLE_MODULES.keys())
            }
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
            "success": "True if generation completed successfully"
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
            {"id": "red_team", "name": "Red Team QA", "description": "Quality assurance and fact-checking"}
        ],
        "available_modules": [
            {
                "id": "exchange_rate",
                "name": "Exchange Rate Analysis",
                "description": "Exchange rate analysis using DataBridges FX, with TradingEconomics as fallback when TE_API_KEY is configured"
            },
            {
                "id": "fuel_energy",
                "name": "Fuel & Energy",
                "description": "Transport fuel price analysis using DataBridges/PriceCache diesel and petrol-gasoline series"
            },
            {
                "id": "livestock_animal_products",
                "name": "Livestock & Animal Products",
                "description": "Livestock, meat, milk, and egg price analysis using DataBridges/PriceCache series"
            },
            {
                "id": "labour_market",
                "name": "Labour Market",
                "description": "Daily wage and staple purchasing-power analysis using DataBridges/PriceCache wage series"
            }
        ]
    }


@router.get("/health")
def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "market-monitor",
        "llm_observability": observability_config().model_dump(),
        "llm_runtime": llm_runtime_status().model_dump(),
    }


@router.get("/countries")
def get_supported_countries():
    """
    Returns the list of countries available in the active PriceCache.
    """
    from .data_loader import get_cache_status_snapshot, get_supported_countries as get_cached_countries

    cache_status = get_cache_status_snapshot()
    return {
        "countries": get_cached_countries(),
        "cache_status": cache_status,
        "warnings": cache_status.get("warnings") or [],
        "operator_warnings": cache_status.get("operator_warnings") or [],
    }


@router.get("/commodities")
def get_commodities(country: Optional[str] = None):
    """
    Returns PriceCache commodities available for a country.
    """
    from .data_loader import (
        get_available_commodities,
        get_commodity_categories,
        normalize_country_name
    )

    if country:
        country_normalized = normalize_country_name(country)
        commodity_list = get_available_commodities(country_normalized)
        categories = get_commodity_categories(commodity_list)

        return {
            "country": country_normalized,
            "commodities": [{"name": c} for c in commodity_list],
            "categories": categories
        }

    return {
        "commodities": [],
        "categories": {},
        "warning": "Select a country to load PriceCache commodity options.",
    }


@router.get("/countries/{country}/metadata")
def get_country_metadata(country: str):
    """
    Returns all available metadata for a specific country:
    - Available commodities
    - Available regions (Admin1)
    - Available markets
    - Date range of available data
    """
    try:
        from .data_loader import PriceCacheUnavailableError, get_country_metadata as get_cached_country_metadata

        return get_cached_country_metadata(country)
    except PriceCacheUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/countries/{country}/reportable-months")
def get_country_reportable_months(
    country: str,
    basket_version_id: Optional[str] = None,
    primary_basket_version_id: Optional[str] = None,
    include_secondary_basket: bool = False,
    secondary_basket_version_id: Optional[str] = None,
    admin1_list: Optional[List[str]] = Query(default=None),
):
    try:
        from .data_loader import PriceCacheUnavailableError, get_reportable_months
        raw = {
            "basket_version_id": basket_version_id,
            "primary_basket_version_id": primary_basket_version_id,
            "include_secondary_basket": include_secondary_basket,
            "secondary_basket_version_id": secondary_basket_version_id,
            "admin1_list": admin1_list or [],
        }
        input_data = ReportableMonthsInput.model_validate(raw)
        normalized_include_secondary, normalized_secondary_id = normalize_secondary_request(input_data)
        if not any(
            [
                basket_version_id,
                primary_basket_version_id,
                include_secondary_basket,
                secondary_basket_version_id,
                admin1_list,
            ]
        ):
            return get_reportable_months(country)
        return get_reportable_months(
            country,
            basket_version_id=input_data.basket_version_id,
            primary_basket_version_id=input_data.primary_basket_version_id,
            include_secondary_basket=normalized_include_secondary,
            secondary_basket_version_id=normalized_secondary_id,
            admin1_list=input_data.admin1_list,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False, include_context=False))
    except SecondBasketFeatureDisabled as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except BasketVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BasketScopeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PriceCacheUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/countries/{country}/reportable-months/refresh")
def refresh_country_reportable_months(country: str, input_data: ReportableMonthsInput):
    try:
        from .data_loader import PriceCacheUnavailableError, refresh_reportable_months_from_databridges
        normalized_include_secondary, normalized_secondary_id = normalize_secondary_request(input_data)

        if not any(
            [
                input_data.primary_basket_version_id,
                input_data.include_secondary_basket,
                input_data.secondary_basket_version_id,
                input_data.admin1_list,
            ]
        ):
            return refresh_reportable_months_from_databridges(
                country,
                basket_version_id=input_data.basket_version_id,
            )
        return refresh_reportable_months_from_databridges(
            country,
            basket_version_id=input_data.basket_version_id,
            primary_basket_version_id=input_data.primary_basket_version_id,
            include_secondary_basket=normalized_include_secondary,
            secondary_basket_version_id=normalized_secondary_id,
            admin1_list=input_data.admin1_list,
        )
    except SecondBasketFeatureDisabled as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except BasketVersionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PriceCacheUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except BasketScopeValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/countries/{country}/baskets", response_model=BasketConfigurationOutput)
def get_country_food_baskets(country: str):
    try:
        return get_country_baskets_response(country)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/countries/{country}/baskets/primary", response_model=BasketConfigurationOutput)
def save_country_primary_food_basket(country: str, input_data: BasketSaveInput):
    try:
        return save_country_basket_role(country, BasketRole.PRIMARY, input_data)
    except BasketValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/countries/{country}/baskets/secondary", response_model=BasketConfigurationOutput)
def save_country_secondary_food_basket(country: str, input_data: BasketSaveInput):
    try:
        return save_country_basket_role(country, BasketRole.SECONDARY, input_data)
    except SecondBasketFeatureDisabled as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except BasketValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get(
    "/countries/{country}/baskets/{role}/history",
    response_model=BasketHistoryOutput,
)
def get_country_food_basket_role_history(country: str, role: str, limit: int = 20):
    try:
        return list_country_basket_role_history(country, role, limit=limit)
    except BasketValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete(
    "/countries/{country}/baskets/secondary",
    response_model=BasketArchiveOutput,
)
def archive_country_secondary_food_basket(country: str):
    try:
        return archive_country_secondary_basket(country)
    except SecondBasketFeatureDisabled as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.to_dict())
    except BasketValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/countries/{country}/basket")
def get_country_food_basket(country: str):
    try:
        return get_country_basket_response(country)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/countries/{country}/basket")
def save_country_food_basket(country: str, input_data: BasketSaveInput):
    try:
        return save_country_basket(country, input_data)
    except BasketValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/countries/{country}/basket/history")
def get_country_food_basket_history(country: str, limit: int = 20):
    try:
        return list_country_basket_history(country, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
