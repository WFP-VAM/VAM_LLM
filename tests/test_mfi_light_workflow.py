"""Real lightweight contracts and analytical engine with malformed provider stubs."""
import json
import threading
from copy import deepcopy
from types import SimpleNamespace
from collections import Counter
import pytest

from app.services.mfi_drafter import light_graph, light_runtime, light_service
from app.services.mfi_drafter.synthetic_fixtures import SyntheticSpec, build_loaded, build_csv_bytes
from app.services.mfi_drafter.schemas import MFIReleaseControl


@pytest.fixture(scope="module")
def loaded():
    return build_loaded(SyntheticSpec(market_count=2, region_count=1))


class Client:
    def __init__(self, changes=(), fail=None):
        self.changes, self.fail = set(changes), fail
        self.calls, self.packages = Counter(), []
        self.lock = threading.Lock()

    def count(self, messages, schema, timeout):
        return sum(len(str(m.content)) for m in messages)//3

    def generate(self, messages, schema, timeout):
        p = json.loads(messages[0].content.split("\nREQUEST:\n",1)[1])
        specs = p["requested_sections"]
        ids = [s["section_id"] if isinstance(s,dict) else s for s in specs]
        family = "summary" if "executive_summary" in ids else "dimensions" if ids[0] in {"Assortment","Availability","Price","Resilience","Competition","Infrastructure","Service","Food Quality","Access & Protection"} else "markets"
        kind = "review" if "needs_revision" in schema["properties"] else "correct" if "REVIEW_REPORT" in p else "draft"
        key = kind+"_"+family
        with self.lock:
            self.calls[key] += 1
            self.packages.append((key, p))
        if self.fail == key:
            raise TimeoutError("injected transient provider failure")
        if kind == "review":
            response = {"needs_revision":family in self.changes,"review_markdown":"Check the evidence and correct the affected passage."}
        else:
            response = {"sections":[{"section_id":sid,"text_markdown":f"{kind}: supported analysis for {sid}."} for sid in ids],"notes":[]}
        return SimpleNamespace(content=json.dumps(response), response_metadata={"finish_reason":"STOP"},usage_metadata={"input_tokens":100,"output_tokens":50,"total_tokens":150})


RELEASE = MFIReleaseControl(analysis_version="2",enabled=True,configuration_status="configured")


@pytest.fixture
def args(monkeypatch, loaded):
    monkeypatch.setattr(light_runtime.time, "sleep", lambda _: None)
    monkeypatch.setattr(light_graph,"retrieve_context",lambda base:{"sources":{},"document_references":[],"contextual_documents":[],"context_status":{},"context_limitation":"No context"})
    monkeypatch.setattr(light_graph,"render_figures",lambda base:{"visualizations":{},"figure_metadata":{}})
    return dict(country=loaded["country"],data_collection_start=loaded["data_collection_start"],data_collection_end=loaded["data_collection_end"],
        markets=loaded["markets"],csv_data=deepcopy(loaded),run_id="light-test",release_control=RELEASE)


@pytest.fixture
def api(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.services.mfi_drafter import router
    from app.shared import async_runs
    monkeypatch.setenv("MFI_DRAFTER_ANALYSIS_VERSION", "2")
    monkeypatch.setattr(async_runs, "_BACKEND", "memory")
    app = FastAPI(); app.include_router(router.router, prefix="/mfi-drafter")
    return TestClient(app)


@pytest.mark.parametrize("changes,expected",[((),5),(("dimensions",),6),(("markets",),6),(("dimensions","markets"),7)])
def test_five_six_seven_calls_and_complete_report(args,changes,expected):
    client=Client(changes)
    result=light_service.run_mfi_report_generation(**args,client=client)
    assert sum(client.calls.values()) == result["llm_calls"] == expected
    phases = result["generation_diagnostics"]["phases"]
    assert len(phases) == 11 and all(p["status"] == "succeeded" for p in phases)
    assert result["generation_diagnostics"]["progress_pct"] == 100
    assert result["llm_diagnostics"]["status"] == "completed"
    assert result["coverage"]["complete"]
    assert len(result["light_narrative"]["dimensions"]) == 9
    assert len(result["light_narrative"]["markets"]) == 2
    assert result["workflow_revision"] == "mfi-light-v1"
    assert result["narrative_schema_version"] == "3.0"
    for kind,p in client.packages:
        if kind.startswith("correct"):
            assert p["ORIGINAL_DRAFT"] and p["REVIEW_REPORT"]["needs_revision"] and p["EVIDENCE"]


def test_failed_phase_is_reported_and_the_whole_report_fails(args):
    client=Client(["dimensions"], fail="review_dimensions")
    steps=[]
    with pytest.raises(TimeoutError):
        light_service.run_mfi_report_generation(**args,client=client,on_step=lambda name, state: steps.append((name, state)))
    assert client.calls["review_dimensions"] == 2  # one transient retry, then the run fails
    assert client.calls["correct_dimensions"] == 0 and client.calls["draft_summary"] == 0
    phases = {p["node"]: p["status"] for p in steps[-1][1]["generation_diagnostics"]["phases"]}
    assert phases["review_dimensions"] == "failed"
    assert phases["draft_dimensions"] == phases["draft_markets"] == "succeeded"
    assert phases["executive_summary"] == phases["assemble_report"] == "pending"


def test_charts_render_alongside_the_drafts(args, monkeypatch):
    # Each side waits for the other, which only succeeds if both run in the same graph step.
    drafting, charting = threading.Event(), threading.Event()
    class Drafting(Client):
        def generate(self, messages, schema, timeout):
            packet = json.loads(messages[0].content.split("\nREQUEST:\n",1)[1])
            if not {"ORIGINAL_DRAFT", "FINAL_DIMENSIONS"} & set(packet):
                drafting.set()
                assert charting.wait(20), "The first drafts must not start after the charts step"
            return super().generate(messages, schema, timeout)
    def charts(base):
        charting.set()
        assert drafting.wait(20), "Charts must render while the drafts are being written"
        return {"visualizations": {}, "figure_metadata": {}}
    monkeypatch.setattr(light_graph, "render_figures", charts)
    assert light_service.run_mfi_report_generation(**args, client=Drafting())["success"]


def test_disabled_release_stops_before_the_graph_is_built(args, monkeypatch):
    from app.services.mfi_drafter.features import MFIAnalysisVersionDisabled, MFI_DRAFTER_ANALYSIS_VERSION_ENV
    def forbidden(*_args, **_kwargs):
        raise AssertionError("the graph must not be built")
    monkeypatch.delenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, raising=False)
    monkeypatch.setattr(light_graph, "build_graph", forbidden)
    client = Client()
    with pytest.raises(MFIAnalysisVersionDisabled):
        light_service.run_mfi_report_generation(**{**args, "release_control": None}, client=client)
    assert not client.calls


def test_supplied_release_snapshot_outlives_a_later_configuration_change(args, monkeypatch):
    from app.services.mfi_drafter.features import MFI_DRAFTER_ANALYSIS_VERSION_ENV, mfi_release_control
    control = mfi_release_control({MFI_DRAFTER_ANALYSIS_VERSION_ENV: "2", "K_REVISION": "candidate-7"})
    monkeypatch.setenv(MFI_DRAFTER_ANALYSIS_VERSION_ENV, "invalid-after-submit")
    result = light_service.run_mfi_report_generation(**{**args, "release_control": control}, client=Client())
    assert result["release_control"]["analysis_version"] == "2"
    assert result["release_control"]["deployment_revision"] == "candidate-7"


def test_map_preflight_reports_same_configuration_error_in_api_and_streamlit(args, api, monkeypatch):
    from app.services.mfi_drafter import router, map_basemap
    from app.streamlit_backend import dispatcher
    monkeypatch.setattr(router, 'load_mfi_from_csv', lambda **kw: args['csv_data'])
    monkeypatch.setattr(dispatcher, 'load_mfi_from_csv', lambda **kw: args['csv_data'])
    monkeypatch.setattr(map_basemap, 'verified_manifest', lambda: (_ for _ in ()).throw(map_basemap.MFICartographyError('MFI offline cartography is corrupt')))
    reply = api.post('/mfi-drafter/generate-from-csv-async', files={'file': ('input.csv', b'placeholder', 'text/csv')})
    assert reply.status_code == 503 and 'cartography' in reply.json()['detail']
    monkeypatch.setattr(dispatcher, '_extract_file', lambda *a: SimpleNamespace(filename='input.csv',content=b'placeholder'))
    with pytest.raises(dispatcher.LocalHTTPException) as error:
        dispatcher._mfi_drafter_generate_from_csv_async(data={}, files={}, params={})
    assert error.value.status_code == 503 and 'cartography' in error.value.detail


def test_http_and_streamlit_share_results_and_recovery_endpoints_are_gone(args, api):
    from app.streamlit_backend import dispatcher
    from app.shared import async_runs
    from docx import Document
    from io import BytesIO
    result = light_service.run_mfi_report_generation(**args, client=Client())
    async_runs.create_run(args["run_id"])
    async_runs.set_run_completed(args["run_id"], result=result)
    reply = api.get("/mfi-drafter/result/"+args["run_id"])
    assert reply.status_code == 200, reply.text
    assert reply.json()["narrative_schema_version"] == "3.0"
    local = dispatcher._mfi_drafter_result(args["run_id"])
    assert json.loads(local.content)["light_narrative"] == reply.json()["light_narrative"]
    status = api.get("/mfi-drafter/status/"+args["run_id"]).json()
    assert status["status"] == "completed" and status["progress_pct"] == 100
    assert not {"resumable", "draft_available", "recovery_limitation", "light_progress"} & set(status)
    for path, method in (("resume", "post"), ("draft", "get"), ("analysis", "get"), ("export-draft-docx", "post")):
        assert getattr(api, method)(f"/mfi-drafter/{path}/{args['run_id']}").status_code == 404
        assert dispatcher.dispatch_request(method.upper(), f"/mfi-drafter/{path}/{args['run_id']}").status_code == 404
    response = api.post("/mfi-drafter/export-docx/"+args["run_id"], json={})
    assert response.status_code == 200, response.text[:300]
    text = "\n".join(p.text for p in Document(BytesIO(response.content)).paragraphs)
    assert "Analytical annex" in text and "INCOMPLETE" not in text
    assert all(d in text for d in result["light_narrative"]["dimensions"])


def test_failed_async_run_is_reported_without_a_result(args, api, monkeypatch):
    from app.services.mfi_drafter import router
    monkeypatch.setattr(router, "load_mfi_from_csv", lambda **kw: args["csv_data"])
    monkeypatch.setattr(router, "run_mfi_report_generation",
        lambda **kw: light_service.run_mfi_report_generation(**kw, client=Client(fail="review_markets")))
    run_id = api.post("/mfi-drafter/generate-from-csv-async", files={"file": ("input.csv", b"placeholder", "text/csv")}).json()["run_id"]
    status = api.get(f"/mfi-drafter/status/{run_id}").json()
    assert status["status"] == "failed" and "injected transient provider failure" in status["error"]
    phases = {p["node"]: p["status"] for p in status["metadata"]["generation_diagnostics"]["phases"]}
    assert phases["review_markets"] == "failed"
    assert api.get(f"/mfi-drafter/result/{run_id}").status_code == 400


def test_json_generation_endpoints_are_removed(args, api):
    from app.streamlit_backend import dispatcher
    body = {k: args[k] for k in ("country", "data_collection_start", "data_collection_end", "markets")}
    for path in ("/mfi-drafter/generate", "/mfi-drafter/generate-async"):
        assert api.post(path, json={**body, "use_mock_data": True}).status_code == 404
        assert dispatcher.dispatch_request("POST", path, json_body=body).status_code == 404


@pytest.mark.parametrize("entrypoint", ["api", "streamlit"])
@pytest.mark.parametrize("dataset", ["synthetic", "Benin"])
def test_csv_submission_schedules_exactly_one_run(api, monkeypatch, entrypoint, dataset):
    from pathlib import Path
    from fastapi import BackgroundTasks
    from app.streamlit_backend import dispatcher
    if dataset == "Benin":
        path = Path(__file__).resolve().parents[1] / "MFI Test Databases/MFI_Full_Benin_surveyid5896.csv"
        if not path.exists():
            pytest.skip("Local Benin benchmark absent")
        content = path.read_bytes()
    else:
        content = build_csv_bytes(SyntheticSpec(market_count=1, region_count=1))
    scheduled = []
    if entrypoint == "api":
        monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, target: scheduled.append(target))
        submit = api.post("/mfi-drafter/generate-from-csv-async", files={"file": ("mfi.csv", content, "text/csv")})
        get_status = lambda run_id: api.get(f"/mfi-drafter/status/{run_id}")
    else:
        monkeypatch.setattr(dispatcher, "threading", SimpleNamespace(
            Thread=lambda target, **kwargs: SimpleNamespace(start=lambda: scheduled.append(target))))
        submit = dispatcher.dispatch_request("POST", "/mfi-drafter/generate-from-csv-async",
                                             files={"file": ("mfi.csv", content, "text/csv")})
        get_status = lambda run_id: dispatcher.dispatch_request("GET", f"/mfi-drafter/status/{run_id}")
    assert submit.status_code == 200, submit.json()
    assert len(scheduled) == 1
    status = get_status(submit.json()["run_id"])
    assert status.status_code == 200 and status.json()["status"] == "pending"
    assert status.json()["metadata"]["workflow_revision"] == "mfi-light-v1"


@pytest.mark.parametrize("country,survey", [("Benin",5896), ("Haiti",5899)])
def test_full_country_packages_and_unchanged_analysis(args, country, survey):
    from pathlib import Path
    from app.services.mfi_drafter.data_loader import load_mfi_from_csv
    root=Path(__file__).resolve().parents[1]
    path=root/f"MFI Test Databases/MFI_Full_{country}_surveyid{survey}.csv"
    if not path.exists(): pytest.skip("Local confidential benchmark absent")
    data=load_mfi_from_csv(path)
    args.update(csv_data=data,country=country,markets=data["markets"],data_collection_start=data["data_collection_start"],data_collection_end=data["data_collection_end"])
    client=Client(["dimensions", "markets"])
    result=light_service.run_mfi_report_generation(**args,client=client)
    expected=json.loads((root/"tests/fixtures/mfi_reliable_baseline.json").read_text(encoding="utf-8"))[country]
    profile=result["assessment_profile"]
    assert profile["assessed_market_count"] == expected["count"]
    assert len(result["excluded_market_records"]) == expected["excluded"]
    assert profile["mean_mfi_across_assessed_markets"] == expected["mean"]
    assert profile["priority_market_names"] == expected["selected"]
    assert profile["priority_dimension_names"] == expected["priorities"]
    assert result["llm_calls"] == 7
    for kind, packet in client.packages:
        ev=packet["EVIDENCE"]
        assert len(ev["market_scores"]["rows"]) == expected["count"]
        if kind.endswith("markets"):
            assert ev["local_evidence"]["rows"] and ev["indicator_definitions"]["rows"]
            assert len({tuple(r[:2]) for r in ev["local_evidence"]["rows"]}) == len(ev["local_evidence"]["rows"])
        if kind.endswith("dimensions") and country == "Benin":
            dims={d["dimension"]:d for d in ev["dimensions"]}
            service=dims["Service"]["facts"]["fact.assessment.service.at_or_below_median"]
            assert (service["numerator"],service["denominator"]) == (37,53)
            assert dims["Infrastructure"]["facts"]["fact.assessment.infrastructure.below_3"]["numerator"] == 7
            assert dims["Food Quality"]["facts"]["fact.region.food_quality.minimum"]["value"] == pytest.approx(4.791666667)
    assert result["coverage"]["complete"]


def test_oversized_groups_split_without_repeating_successful_sections(args):
    class Limited(Client):
        def count(self,messages,schema,timeout):
            packet=json.loads(messages[0].content.split("\nREQUEST:\n",1)[1])
            return 250001 if len(packet["requested_sections"]) > 4 else 100
    client=Limited()
    result=light_service.run_mfi_report_generation(**args,client=client)
    assert len(result["light_narrative"]["dimensions"]) == 9
    ids=[s["section_id"] for kind, p in client.packages if kind=="draft_dimensions" for s in p["requested_sections"]]
    assert len(ids)==len(set(ids))==9


@pytest.mark.parametrize("variant", ["renamed", "unicode", "sparse"])
def test_country_identity_and_sparse_variants_preserve_official_scores(args, loaded, variant):
    from app.services.mfi_drafter.synthetic_fixtures import build_dataframe
    from app.services.mfi_drafter.data_loader import load_mfi_from_dataframe
    frame=build_dataframe(SyntheticSpec(country="Alternative Country",market_count=2,region_count=1,
        include_item_drivers=variant != "sparse", include_category_drivers=variant != "sparse"))
    if variant == "unicode":
        frame["MarketID"] = frame.MarketName.map({"Market 01":"01","Market 02":"02"})
        frame["SurveyID"] = "100"
        frame["MarketName"] = "São José — Marché"
    data=load_mfi_from_dataframe(frame)
    args.update(country=data["country"],csv_data=data,markets=data["markets"])
    client=Client()
    result=light_service.run_mfi_report_generation(**args,client=client)
    assert sorted(m["overall_mfi"] for m in data["markets_data"]) == sorted(m["overall_mfi"] for m in loaded["markets_data"])
    assert result["llm_calls"] == 5 and result["coverage"]["complete"]
    assert len(result["light_narrative"]["markets"]) == 2
    assert all(p["EVIDENCE"]["country"] == "Alternative Country" for _,p in client.packages)
