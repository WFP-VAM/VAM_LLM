from __future__ import annotations


from app.services.mfi_drafter.schemas import MFIGenerationDiagnostics, MFIQAReview


def test_public_qa_and_diagnostic_contracts_expose_figure_delivery_state() -> None:
    review = MFIQAReview(
        status="delivered_with_unverified_figures",
        unverified_figure_flag_ids=["numeric-16"],
        unverified_figure_claim_ids=["dimension.price.geography.2"],
        unverified_figure_values=["16"],
    )
    diagnostics = MFIGenerationDiagnostics(
        delivery_qa_status="delivered_with_unverified_figures",
        unresolved_high_count=1,
        blocking_high_count=0,
        unverified_figure_flag_count=1,
        unverified_figure_claim_count=1,
        unverified_figure_flag_ids=["numeric-16"],
        unverified_figure_values=["16"],
    )

    assert review.status == "delivered_with_unverified_figures"
    assert diagnostics.unresolved_high_count == 1
    assert diagnostics.blocking_high_count == 0
