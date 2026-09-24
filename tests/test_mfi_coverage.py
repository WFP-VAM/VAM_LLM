from app.services.mfi_drafter.coverage import evaluate_coverage, table_block


def test_coverage_counts_only_requirements_satisfied_by_visible_blocks():
    profile = {"coverage_manifest": [{"requirement_id": "r", "dimension": "Price"}]}
    assert not evaluate_coverage(profile, [])["complete"]
    table = table_block("Evidence", [{"value": "5.00"}], [("value", "Score")], requirements=["r"])
    coverage = evaluate_coverage(profile, [table])
    assert coverage["complete"] and coverage["covered"] == coverage["total"] == 1
    assert coverage["requirements"][0]["satisfied_by"] == ["block:0"]
