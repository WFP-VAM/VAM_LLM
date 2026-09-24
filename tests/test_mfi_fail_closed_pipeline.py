from __future__ import annotations


from app.shared.llm_observability import LLMCallError


def test_llm_error_public_contract_includes_active_task_or_batch() -> None:
    error = LLMCallError(
        failure_code="llm_transport_error",
        call_id="llm-1",
        node="red_team",
        operation="mfi.red_team_review.v6",
        stage="transport",
        batch_id="batch-1",
    )
    assert error.to_public_dict()["batch_id"] == "batch-1"
    assert error.to_public_dict()["call_id"] == "llm-1"
