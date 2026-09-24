from __future__ import annotations

import csv
import io
import json

import pytest
from pydantic import ValidationError

import streamlit_shared as shared
from app.services.mfi_drafter.synthetic_fixtures import SyntheticSpec, build_profile
from app.services.mfi_drafter.table_projection import (
    MFIReportTableColumn,
    MFIReportTableSpec,
    build_mfi_raw_table_downloads,
)


@pytest.fixture(scope="module")
def profile() -> dict:
    return build_profile(
        SyntheticSpec(market_count=20, item_market_ratio=0.5)
    ).model_dump(mode="python")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {
                "spec_id": "duplicate",
                "canonical_source_table": "rows",
                "columns": (
                    MFIReportTableColumn(key="a", label="A"),
                    MFIReportTableColumn(key="a", label="Again"),
                ),
            },
            "unique",
        ),
        (
            {
                "spec_id": "too-wide",
                "canonical_source_table": "rows",
                "columns": tuple(
                    MFIReportTableColumn(key=f"c{i}", label=f"C{i}")
                    for i in range(9)
                ),
            },
            "eight",
        ),
        (
            {
                "spec_id": "bad-cap",
                "canonical_source_table": "rows",
                "columns": (MFIReportTableColumn(key="a", label="A"),),
                "maximum_rows": 0,
            },
            "positive",
        ),
    ],
)
def test_table_spec_validation(kwargs, message) -> None:
    with pytest.raises(ValidationError, match=message):
        MFIReportTableSpec(**kwargs)


def test_column_width_and_text_validation() -> None:
    with pytest.raises(ValidationError, match="positive"):
        MFIReportTableColumn(key="a", label="A", width_hint=0)
    with pytest.raises(ValidationError, match="non-empty"):
        MFIReportTableColumn(key=" ", label="A")


def test_raw_downloads_are_complete_full_precision_and_deterministic(profile) -> None:
    first = build_mfi_raw_table_downloads(profile)
    second = build_mfi_raw_table_downloads(profile)
    assert first == second
    assert len(first) == 7
    assert len({item["file_name"] for item in first}) == 7
    bundle = json.loads(first[0]["data"].decode("utf-8"))
    assert bundle == profile["tables"]
    dimension_csv = next(
        item for item in first if item["file_name"] == "mfi_dimension_raw.csv"
    )
    csv_rows = list(
        csv.DictReader(io.StringIO(dimension_csv["data"].decode("utf-8-sig")))
    )
    canonical = profile["tables"]["dimension_rows"][0]
    assert csv_rows[0]["row_id"] == canonical["row_id"]
    assert float(csv_rows[0]["mean"]) == canonical["values"]["mean"]
    assert csv_rows[0]["mean"] != f"{canonical['values']['mean']:.2f}"


def test_technical_download_renderer_exposes_json_and_six_csvs(profile, monkeypatch) -> None:
    buttons = []
    monkeypatch.setattr(shared.st, "markdown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(shared.st, "caption", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        shared.st,
        "download_button",
        lambda label, **kwargs: buttons.append((label, kwargs)),
    )
    shared.render_mfi_raw_table_downloads(profile, key_prefix="r6")
    assert len(buttons) == 7
    assert buttons[0][1]["file_name"].endswith(".json")
    assert all(item[1]["file_name"].endswith(".csv") for item in buttons[1:])
    assert len({item[1]["key"] for item in buttons}) == 7
