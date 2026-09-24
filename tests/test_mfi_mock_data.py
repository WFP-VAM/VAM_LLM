"""Synthetic MFI data, used only when a report is explicitly generated without a CSV."""
import random

from app.services.mfi_drafter.analysis import build_assessment_profile
from app.services.mfi_drafter.mock_data import generate_mock_mfi_data
from app.services.mfi_drafter.schemas import MFI_DIMENSIONS

MARKETS = ["Market A", "Market B", "Market C"]


def test_mock_data_is_labelled_synthetic_and_reproducible_with_a_seed():
    random.seed(7)
    first = generate_mock_mfi_data("Testland", MARKETS, "2026-01-01", "2026-01-31")
    random.seed(7)
    assert generate_mock_mfi_data("Testland", MARKETS, "2026-01-01", "2026-01-31") == first
    assert first["score_authority"] == "synthetic_mock"
    assert [m["market_name"] for m in first["markets_data"]] == MARKETS
    assert set(first["metric_summaries"]) == set(MFI_DIMENSIONS)
    assert first["survey_metadata"]["total_markets"] == 3
    for market in first["markets_data"]:
        assert market["admin0"] == "Testland"
        assert all(4.5 <= score <= 9.5 for score in market["dimension_scores"].values())
        assert all(m["methodology_note"].startswith("Synthetic mock evidence")
                   for metrics in market["subsections"].values() for m in metrics)


def test_mock_data_feeds_the_deterministic_analysis():
    random.seed(11)
    data = generate_mock_mfi_data("Testland", MARKETS[:2], "2026-01-01", "2026-01-31")
    profile = build_assessment_profile(data["markets_data"], data["metric_summaries"], data).model_dump(mode="json")
    assert profile["assessed_market_count"] == 2
    assert len(profile["dimensions"]) == len(MFI_DIMENSIONS)
