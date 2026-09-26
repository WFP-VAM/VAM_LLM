"""Node prepare_analysis: the deterministic assessment profile of the uploaded CSV."""
from __future__ import annotations
from copy import deepcopy
from ..contracts import WORKFLOW


def prepare_analysis(inputs):
    from ..analysis import build_assessment_profile
    loaded = inputs["csv_data"]
    profile =build_assessment_profile(loaded["markets_data"], loaded["metric_summaries"], loaded).model_dump(mode="json")
    return {**{k: inputs[k] for k in ("country", "data_collection_start", "data_collection_end", "run_id")},
        **{k: deepcopy(loaded.get(k)) for k in ("markets_data", "metric_summaries", "survey_metadata", "score_authority", "methodology_version",
            "excluded_market_records", "methodology_warnings", "warnings")},
        "assessment_profile": profile, "workflow_revision": WORKFLOW, "analysis_schema_version": "2.1",
        "narrative_schema_version": "3.0", "mean_mfi_across_assessed_markets": profile["mean_mfi_across_assessed_markets"],
        "release_control": inputs["release_control"]}
