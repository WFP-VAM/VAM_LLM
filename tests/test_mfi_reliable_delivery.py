from app.services.mfi_drafter.coverage import body_projection, evaluate_coverage, table_block
from app.services.mfi_drafter.facts import subject_binding_problems


def test_body_projection_keeps_canonical_analysis_and_coverage_checks_visible_blocks():
    source = {"Price": {"key_findings": list(range(6)), "subdimension_analysis": list(range(4)), "geographic_patterns": list(range(3))}}
    body = body_projection(source)
    assert list(map(len, body["Price"].values())) == [3, 2, 2]
    assert len(source["Price"]["key_findings"]) == 6
    profile = {"coverage_manifest": [{"requirement_id": "r", "dimension": "Price"}]}
    assert not evaluate_coverage(profile, [])["complete"]
    table = table_block("Evidence", [{"value": "5.00"}], [("value", "Score")], requirements=["r"])
    assert evaluate_coverage(profile, [table])["complete"]


def test_swapped_named_market_scores_fail():
    entries = [{"market_name": "Market A", "unit": "score", "numeric_value": 3}, {"market_name": "Market B", "unit": "score", "numeric_value": 7}]
    assert len(subject_binding_problems("Market A scored 7/10 and Market B scored 3/10.", entries)) == 2
    assert not subject_binding_problems("Market A scored 3/10 and Market B scored 7/10.", entries)
