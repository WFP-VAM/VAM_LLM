"""Real graph-node validators and journals, with controlled model responses."""

from app.services.mfi_drafter.analysis import build_assessment_profile
from app.services.mfi_drafter.synthetic_fixtures import build_loaded, SyntheticSpec


def test_country_renaming_changes_identity_but_not_equivalent_numerical_analysis():
    profiles, inputs = [], []
    for country in ("Test Republic", "République Ω"):
        loaded = build_loaded(SyntheticSpec(country=country,market_count=3,region_count=2,item_market_ratio=.5))
        inputs.append(loaded)
        profiles.append(build_assessment_profile(loaded["markets_data"],loaded["metric_summaries"],loaded).model_dump())
    assert {m["market_key"] for m in inputs[0]["markets_data"]}.isdisjoint({m["market_key"] for m in inputs[1]["markets_data"]})
    assert profiles[0]["mean_mfi_across_assessed_markets"] == profiles[1]["mean_mfi_across_assessed_markets"]
    assert profiles[0]["priority_market_names"] == profiles[1]["priority_market_names"]
    assert [d["statistics"] for d in profiles[0]["dimensions"]] == [d["statistics"] for d in profiles[1]["dimensions"]]
