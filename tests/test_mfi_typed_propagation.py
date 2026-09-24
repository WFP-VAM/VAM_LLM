from __future__ import annotations


from app.services.mfi_drafter import router
from app.streamlit_backend import dispatcher


def test_analysis_metadata_is_exposed_by_router_and_dispatcher_helpers():
    profile = {
        "analysis_version": "mfi-analysis-phase2-v1",
        "analysis_schema_version": "2.1",
        "priority_dimension_names": ["Service"],
        "priority_market_names": ["Juba"],
        "limitations": [{"code": "assessment_scope_not_representative"}],
    }
    state = {
        "assessment_profile": profile,
        "methodology_warnings": [{"code": "mfir_records_excluded"}],
        "narrative_schema_version": "3.0",
    }

    router_metadata = router._analysis_run_metadata(state)
    dispatcher_metadata = dispatcher._mfi_analysis_run_metadata(state)

    assert router_metadata == dispatcher_metadata
    assert router_metadata["analysis_version"] == "mfi-analysis-phase2-v1"
    assert router_metadata["priority_dimension_names"] == ["Service"]
    assert router_metadata["priority_market_names"] == ["Juba"]
    assert router_metadata["analysis_limitations"][0]["code"] == (
        "assessment_scope_not_representative"
    )
    assert router_metadata["methodology_warnings"][0]["code"] == (
        "mfir_records_excluded"
    )
    assert router_metadata["narrative_schema_version"] == "3.0"
