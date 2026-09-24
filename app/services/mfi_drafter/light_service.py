"""Public lightweight MFI execution, shared by HTTP and in-process dispatch."""
from __future__ import annotations
import inspect
import uuid
from importlib.metadata import version
from .errors import MFIRunError
from .light_contracts import WORKFLOW, BUNDLE, MODEL, MAX_CHARACTERS, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, NODES
from .light_runtime import RunLedger, public_diagnostics


def effective_contract():
    from .light_contracts import instructions, response_schema
    from .reliable_contracts import fingerprint
    return {"workflow": WORKFLOW, "bundle": BUNDLE, "model": MODEL, "location": "global", "temperature": 1.0,
        "analysis_schema": "2.1", "narrative_schema": "3.0", "methodology": "databridge-current",
        "timeout": 600, "summary_timeout": 180, "max_attempts": 2, "sdk_retries": 0,
        "max_characters": MAX_CHARACTERS, "max_input_tokens": MAX_INPUT_TOKENS, "max_output_tokens": MAX_OUTPUT_TOKENS,
        "schema_hashes": {"sections": fingerprint(response_schema()), "review": fingerprint(response_schema(True))},
        "prompt_hashes": {node: fingerprint(instructions(node)) for node in NODES if node.startswith(("draft_", "review_", "correct_")) or node == "executive_summary"},
        "dependencies": {name:version(name) for name in ("pydantic", "langgraph", "langchain-core", "langchain-google-vertexai")}}


def runtime_status():
    return {"status": "configured", "provider": "vertex_ai", **effective_contract(),
            "access_validation": "performed_on_invocation"}


PHASE_DESCRIPTIONS = {
    "prepare_analysis": "Builds the deterministic assessment profile from the CSV",
    "context_retrieval": "Retrieves ReliefWeb and Seerist context documents",
    "charts": "Renders the charts and maps",
    "draft_dimensions": "Drafts the dimension sections",
    "draft_markets": "Drafts the market sections",
    "review_dimensions": "Reviews the dimension drafts",
    "review_markets": "Reviews the market drafts",
    "correct_dimensions": "Revises the dimension drafts when the review asks for it",
    "correct_markets": "Revises the market drafts when the review asks for it",
    "executive_summary": "Drafts the executive summary and country context",
    "assemble_report": "Assembles the report blocks",
}


def service_info():
    """Service metadata shared by the HTTP router and the in-process dispatcher."""
    from app.shared.llm_observability import observability_config
    from .features import mfi_release_control
    from .schemas import MFI_DIMENSIONS
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
        "llm_runtime": runtime_status(),
        "supports_csv_upload": True,
        "data_source": "Uploaded processed MFI CSV",
        "csv_upload": {
            "endpoint": "/generate-from-csv",
            "async_endpoint": "/generate-from-csv-async",
            "validate_endpoint": "/validate-csv",
            "required_columns": ["MarketName", "Adm0Name", "Adm1Name", "LevelID", "DimensionName",
                                 "VariableName", "OutputValue", "TradersSampleSize"],
            "optional_columns": ["MarketLatitude", "MarketLongitude", "Adm2Name", "StartDate", "EndDate"],
            "description": "Upload the final processed or elaborated MFI CSV to generate the report.",
        },
        "outputs": {
            "run_id": "Unique generation identifier",
            "workflow_revision": "Workflow that produced the report",
            "release_control": "Deployment-control snapshot",
            "assessment_profile": "Deterministic scores, rankings, limitations and tables",
            "mean_mfi_across_assessed_markets": "Unweighted mean MFI across the assessed markets",
            "market_score_distribution": "Assessed-market scores in order",
            "light_narrative": "Final dimension, market and summary sections",
            "review_reports": "Reviews of the dimension and market drafts",
            "report_blocks": "Reader-facing report content, also used for the DOCX export",
            "visualizations": "Charts and maps in Base64 format",
            "context_status": "Context retrieval status",
            "document_references": "Context documents available to the report",
            "generation_diagnostics": "Phase status, progress and model attempts",
            "llm_diagnostics": "Model call trace summary",
            "success": "True if generation is completed",
        },
        "workflow_nodes": [
            {"id": node, "name": node.replace("_", " ").capitalize(), "description": PHASE_DESCRIPTIONS[node]}
            for node in NODES
        ],
        "mfi_dimensions": MFI_DIMENSIONS,
    }


def inputs_for(**kwargs):
    bound = inspect.signature(run_mfi_report_generation).bind(**kwargs)
    bound.apply_defaults()
    return {k:v for k,v in bound.arguments.items() if k not in {"on_step", "llm_trace_sink", "release_control", "client"}}


def validate_submission(csv_data):
    """Refuse a CSV submission up front when its report could not be drawn."""
    from .map_basemap import preflight_maps, MFICartographyError
    try:
        preflight_maps(csv_data)
    except MFICartographyError as exc:
        # Existing HTTP/Streamlit handlers preserve this explicit configuration cause.
        raise MFIRunError(str(exc), 503) from exc


def run_mfi_report_generation(country, data_collection_start, data_collection_end, markets, csv_data,
        on_step=None, release_control=None, run_id=None, llm_trace_sink=None, *, client=None):
    from .features import require_mfi_analysis_v2
    from .light_graph import build_graph
    from .map_basemap import preflight_maps
    control = require_mfi_analysis_v2(release_control)
    preflight_maps(csv_data)
    run_id = run_id or "mfi_"+uuid.uuid4().hex[:8]
    inputs = inputs_for(country=country, data_collection_start=data_collection_start, data_collection_end=data_collection_end,
        markets=markets, csv_data=csv_data, run_id=run_id)
    ledger = RunLedger(run_id)
    ledger.change(lambda v: v.update(light_phases={name: {"status": "pending"} for name in NODES}))
    graph = build_graph(ledger, client=client, on_step=on_step, trace_sink=llm_trace_sink)
    try:
        result = graph.invoke({"base": {**inputs, "release_control": control.model_dump()}}, config={"recursion_limit": 30})["report"]
    except Exception:
        ledger.finish("failed")
        raise
    ledger.finish("completed")
    diagnostics = public_diagnostics(ledger.read())
    result["llm_diagnostics"] = {**diagnostics.pop("llm_diagnostics"), "status": "completed"}
    result["generation_diagnostics"] = diagnostics
    result["response_contract_bundle"] = BUNDLE
    result["effective_contract"] = effective_contract()
    result["llm_calls"] = diagnostics["model_attempt_total"]
    return result
