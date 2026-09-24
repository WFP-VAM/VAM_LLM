"""Prepare ten private acceptance inputs; explicitly run them on the owner's test deployment."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def prepare(directory, datasets):
    from app.services.mfi_drafter.synthetic_fixtures import SyntheticSpec, build_dataframe
    directory.mkdir(parents=True, exist_ok=True)
    cases = []
    for country, survey in (("Benin", 5896), ("Haiti", 5899)):
        source = datasets / f"MFI_Full_{country}_surveyid{survey}.csv"
        for index in range(1, 4):
            case_id = f"{country.lower()}-{index}"
            target = directory / (case_id+".csv")
            shutil.copyfile(source, target)
            cases.append({"id": case_id, "file": target.name, "baseline": country})
    specs = [
        ("renamed-country", SyntheticSpec(country="Example Republic", market_count=6)),
        ("unicode-geography", SyntheticSpec(country="Example Islands", market_count=6)),
        ("sparse-indicators", SyntheticSpec(country="Example Highlands", market_count=6,
            include_item_drivers=False, include_category_drivers=False)),
        ("few-markets", SyntheticSpec(country="Example Coast", market_count=2, region_count=1)),
    ]
    for case_id, spec in specs:
        frame = build_dataframe(spec)
        if case_id == "unicode-geography":
            names = frame.MarketName.unique()
            frame["MarketID"] = frame.MarketName.map({n: str(100+i) for i,n in enumerate(names)})
            frame["SurveyID"] = "99901"
            frame["MarketName"] = frame.MarketName.map({n: ("Marché Central" if i % 2 == 0 else "São José") for i,n in enumerate(names)})
            frame["Adm1Name"] = frame.Adm1Name.map(lambda s: "Île "+s)
        target = directory/(case_id+".csv")
        frame.to_csv(target, index=False, encoding="utf-8")
        cases.append({"id": case_id, "file": target.name, "baseline": None})
    for case in cases:
        case["sha256"] = hashlib.sha256((directory/case["file"]).read_bytes()).hexdigest()
        case["semantic_acceptance"] = "pending_manual_review"
    (directory/"manifest.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    return cases


def run(directory, base_url):
    import requests
    cases = json.loads((directory/"manifest.json").read_text(encoding="utf-8"))
    session = requests.Session()
    durations, outcomes = [], []
    base_url = base_url.rstrip("/")
    def call(method, path, **kwargs):
        response = session.request(method, base_url+"/mfi-drafter/"+path, timeout=180, **kwargs)
        response.raise_for_status()
        return response.json()
    for case in cases:
        record_path=directory/(case["id"]+"-run.json")
        record=json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else {"case":case["id"]}
        actual_hash=hashlib.sha256((directory/case["file"]).read_bytes()).hexdigest()
        if actual_hash != case["sha256"] or record.get("sha256", actual_hash) != actual_hash:
            raise ValueError(f"Input changed for {case['id']}; prepare a new acceptance directory")
        started=time.time()
        if "run_id" not in record:
            with (directory/case["file"]).open("rb") as handle:
                submission=call("POST", "generate-from-csv-async", files={"file":(case["file"], handle, "text/csv")})
            record.update(run_id=submission["run_id"], submitted_at=started, sha256=actual_hash)
            record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        while True:
            status=call("GET", "status/"+record["run_id"])
            record["status"]=status
            record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
            if status["status"] in {"completed", "failed"}: break
            if time.time()-started > 3600: break
            time.sleep(5)
        completed=status["status"] == "completed"
        if completed:
            record.setdefault("completed_at", time.time())
        elapsed=record.get("completed_at", time.time())-record["submitted_at"]
        record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        outcome={"case":case["id"], "completed":completed, "seconds":elapsed,
            "semantic_acceptance":"pending_manual_review"}
        if completed:
            result=call("GET", "result/"+record["run_id"])
            (directory/(case["id"]+"-result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            outcome.update(model_calls=result["llm_calls"], coverage_complete=result["coverage"]["complete"])
            assert result["workflow_revision"] == "mfi-light-v1"
            if case["baseline"]:
                expected=json.loads((ROOT/"tests/fixtures/mfi_reliable_baseline.json").read_text(encoding="utf-8"))[case["baseline"]]
                profile=result["assessment_profile"]
                assert profile["assessed_market_count"] == expected["count"]
                assert profile["priority_market_names"] == expected["selected"]
                assert abs(profile["mean_mfi_across_assessed_markets"]-expected["mean"]) <= 1e-6
                outcome["ordinary_call_target_met"] = 5 <= result["llm_calls"] <= 7
            durations.append(elapsed)
        outcomes.append(outcome)
        print(json.dumps(outcome), flush=True)
        summary={"runs":outcomes, "completed":sum(o["completed"] for o in outcomes),
            "median_seconds":statistics.median(durations) if durations else None,
            "within_15_minutes":sum(d <= 900 for d in durations), "promotion":"requires_all_ten_runs_and_manual_semantic_review"}
        (directory/"results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "run"])
    parser.add_argument("--directory", type=Path, default=ROOT/".tmp/mfi-light-acceptance")
    parser.add_argument("--datasets", type=Path, default=ROOT/"MFI Test Databases")
    parser.add_argument("--url", help="Existing owner-selected test deployment; run makes paid model calls")
    args=parser.parse_args()
    if args.action == "prepare":
        print(f"Prepared {len(prepare(args.directory, args.datasets))} cases in {args.directory}")
    else:
        if not args.url: parser.error("run requires --url")
        run(args.directory, args.url)
