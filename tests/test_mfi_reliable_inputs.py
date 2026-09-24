from pathlib import Path

import pandas as pd
import pytest

from app.services.mfi_drafter.data_loader import load_mfi_from_dataframe
from app.services.mfi_drafter.analysis import build_assessment_profile
from app.services.mfi_drafter.input_validation import MFIInputError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def market_frame():
    path = ROOT / "MFI Test Databases/MFI_Full_Benin_surveyid5896.csv"
    if not path.exists():
        pytest.skip("Local benchmark absent")
    frame = pd.read_csv(path, dtype=str)
    return frame[frame.MarketID == "124"].copy()


def test_aliases_do_not_duplicate_traders(market_frame):
    alias = market_frame.assign(MarketName="Dantokpa alternate")
    data = load_mfi_from_dataframe(pd.concat([market_frame, alias], ignore_index=True))
    assert len(data["markets_data"]) == 1
    assert data["survey_metadata"]["total_traders"] == 24
    assert len(data["markets_data"][0]["identity"]["aliases"]) == 2


def test_shared_names_have_separate_identifiers(market_frame):
    other = market_frame.assign(MarketID="999999")
    data = load_mfi_from_dataframe(pd.concat([market_frame, other], ignore_index=True))
    assert len(data["markets_data"]) == 2
    assert len({m["market_name"] for m in data["markets_data"]}) == 2
    assert len({m["market_key"] for m in data["markets_data"]}) == 2


def test_identity_is_independent_of_row_order(market_frame):
    a = load_mfi_from_dataframe(market_frame)
    b = load_mfi_from_dataframe(market_frame.iloc[::-1])
    for key in ("market_key", "overall_mfi", "dimension_scores", "traders_surveyed", "identity"):
        assert a["markets_data"][0][key] == b["markets_data"][0][key]
    assert a["survey_metadata"] == b["survey_metadata"]


@pytest.mark.parametrize("column,value", [("SurveyID","wrong"),("Adm0Name","Haiti"),("Adm0Code","999")])
def test_mixed_assessment_rejected(market_frame,column,value):
    mutated=market_frame.copy()
    mutated.loc[mutated.index[0],column]=value
    with pytest.raises(MFIInputError,match="multiple assessment"):
        load_mfi_from_dataframe(mutated)


@pytest.mark.parametrize("value",["-5","2.9","broken",None])
def test_invalid_trader_counts_are_unknown(market_frame,value):
    data=load_mfi_from_dataframe(market_frame.assign(TradersSampleSize=value))
    assert data["survey_metadata"]["total_traders"] is None
    assert data["markets_data"][0]["traders_surveyed"] is None
    assert data["input_findings"]


def test_bad_coordinates_do_not_remove_market(market_frame):
    data=load_mfi_from_dataframe(market_frame.assign(MarketLatitude="500",MarketLongitude="-999"))
    assert data["markets_data"][0]["latitude"] is None
    assert data["markets_data"][0]["longitude"] is None
    assert data["markets_data"][0]["overall_mfi"] > 0


def test_bad_optional_value_is_not_non_applicability(market_frame):
    data=market_frame.copy()
    mask=data.VariableName.str.strip() == "QualityRefrigerate"
    assert mask.any()
    data.loc[mask,"OutputValue"]="broken"
    loaded=load_mfi_from_dataframe(data)
    metric=next(m for m in loaded["markets_data"][0]["drivers"]["Food Quality"] if m["variable_name"] == "QualityRefrigerate")
    assert metric["applicability_status"] == "missing"
    assert metric["parsing_status"] == "nonnumeric"
    assert metric["applicability_basis"] == "unknown"
    assert metric["source_values"] == ["broken"]
    assert metric["source_rows"]
    finding = next(item for item in loaded["input_findings"] if "QualityRefrigerate" in item["fields"])
    assert finding["row_references"] == metric["source_rows"]
    assert finding["market_key"] == loaded["markets_data"][0]["market_key"]


def test_conflicting_official_score_has_structured_source_findings(market_frame):
    score = market_frame[market_frame.LevelID == "1"].iloc[[0]].copy()
    score["OutputValue"] = "99"
    with pytest.raises(MFIInputError) as failure:
        load_mfi_from_dataframe(pd.concat([market_frame, score], ignore_index=True))
    assert failure.value.findings[0]["code"] == "invalid_official_scores"
    assert failure.value.findings[0]["market_key"]
    assert failure.value.findings[0]["row_references"]


def test_explicit_non_applicability_remains_distinct_from_empty_value(market_frame):
    frame = market_frame.copy()
    frame["MetricApplicability"] = ""
    selected = frame.VariableName.str.strip() == "QualityRefrigerate"
    frame.loc[selected, "MetricApplicability"] = "not_applicable"
    frame.loc[selected, "OutputValue"] = ""
    loaded = load_mfi_from_dataframe(frame)
    metric = next(m for m in loaded["markets_data"][0]["drivers"]["Food Quality"] if m["variable_name"] == "QualityRefrigerate")
    assert metric["parsing_status"] == "empty"
    assert metric["applicability"] == "not_applicable"
    assert metric["applicability_basis"] == "explicit_source_column"
    profile = build_assessment_profile(loaded["markets_data"], loaded["metric_summaries"], loaded).model_dump()
    quality = next(d for d in profile["dimensions"] if d["dimension"] == "Food Quality")
    summary = next(m for m in quality["drivers"] if m["metric_id"] == metric["metric_id"])
    assert summary["availability"]["classification"] == "not_applicable"
    assert summary["coverage"]["available_market_count"] == 0
