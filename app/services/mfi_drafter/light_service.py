"""Public lightweight MFI execution, shared by HTTP and in-process dispatch."""
from __future__ import annotations
import inspect
import uuid
from importlib.metadata import version
from .errors import MFIRunError
from .light_contracts import WORKFLOW, BUNDLE, MODEL, MAX_CHARACTERS, MAX_INPUT_TOKENS, MAX_OUTPUT_TOKENS, NODES
from .light_runtime import RunLedger, public_diagnostics

MOCK_DATA_REQUIRED = ("No processed MFI CSV was supplied. Upload the CSV, or set use_mock_data=true "
                      "to generate a demonstration report on synthetic data.")


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


def inputs_for(**kwargs):
    bound = inspect.signature(run_mfi_report_generation).bind(**kwargs)
    bound.apply_defaults()
    return {k:v for k,v in bound.arguments.items() if k not in {"on_step", "llm_trace_sink", "release_control", "client", "use_mock_data"}}


def validate_submission(csv_data):
    """Refuse a CSV submission up front when its report could not be drawn."""
    from .map_basemap import preflight_maps, MFICartographyError
    try:
        preflight_maps(csv_data)
    except MFICartographyError as exc:
        # Existing HTTP/Streamlit handlers preserve this explicit configuration cause.
        raise MFIRunError(str(exc), 503) from exc


def run_mfi_report_generation(country, data_collection_start, data_collection_end, markets, csv_data=None,
        on_step=None, release_control=None, run_id=None, llm_trace_sink=None, *, client=None, use_mock_data=False):
    from .features import require_mfi_analysis_v2
    from .light_graph import build_graph
    from .map_basemap import preflight_maps
    control = require_mfi_analysis_v2(release_control)
    if csv_data is None and not use_mock_data:
        raise MFIRunError(MOCK_DATA_REQUIRED, 400)
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
