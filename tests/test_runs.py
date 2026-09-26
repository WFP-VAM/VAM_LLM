"""The shared run infrastructure: the store, the executor's rules for late or dead work, and report runs."""
import base64
import logging
import threading
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import main
from app.services.market_monitor import router as market_monitor_router
from app.shared.runs import report_runs
from app.shared.runs.executor import Superseded, expire_overdue, fenced, launch
from app.shared.runs.store import Conflict, MemoryStore, Missing
from app.streamlit_backend import dispatcher


def record_store(**record):
    store = MemoryStore('runs', missing='Run not found')
    store.mutate('run-1', lambda _current: {'revision': 1, **record})
    return store


def test_fenced_writes_only_while_the_work_holds_the_run():
    store = record_store(status='running', value=0)
    updated = fenced(store, 'run-1', lambda record: record.update(value=1),
                     holds=lambda record: record['status'] == 'running', refused='gone', clock=lambda: 42.0)
    assert (updated['value'], updated['revision'], updated['updated_at']) == (1, 2, 42.0)
    store.mutate('run-1', lambda record: {**record, 'status': 'interrupted'})
    with pytest.raises(Superseded, match='gone') as refused:
        fenced(store, 'run-1', lambda record: record.update(value=2),
               holds=lambda record: record['status'] == 'running', refused='gone', clock=lambda: 43.0)
    assert isinstance(refused.value, Conflict) and store.get('run-1')['value'] == 1
    with pytest.raises(Superseded):
        fenced(store, 'no-such-run', lambda record: None, holds=lambda record: True, refused='gone', clock=lambda: 0)


def test_overdue_work_is_interrupted_once_and_finished_work_is_left_alone():
    now = [100.0]
    overdue = lambda record: record['status'] == 'running' and record['deadline'] <= now[0]  # noqa: E731
    interrupt = lambda record: record.update(status='interrupted')  # noqa: E731
    store = record_store(status='running', deadline=50.0)
    record = expire_overdue(store, 'run-1', store.get('run-1'), overdue=overdue, interrupt=interrupt, clock=lambda: now[0])
    assert (record['status'], record['revision']) == ('interrupted', 2)
    assert expire_overdue(store, 'run-1', record, overdue=overdue, interrupt=interrupt, clock=lambda: now[0]) == record

    # The work finished between the read and the transaction: the fresh record is not overdue any more.
    store = record_store(status='running', deadline=50.0)
    stale = store.get('run-1')
    store.mutate('run-1', lambda current: {**current, 'status': 'completed'})
    assert expire_overdue(store, 'run-1', stale, overdue=overdue, interrupt=interrupt,
                          clock=lambda: now[0])['status'] == 'completed'


def test_launched_work_runs_in_a_daemon_thread_and_its_errors_are_logged(caplog):
    done = threading.Event()

    def fail():
        done.set()
        raise RuntimeError('work broke')
    with caplog.at_level(logging.ERROR, logger='app.shared.runs.executor'):
        launch(fail, name='test-work')
        assert done.wait(5)
        for _ in range(50):
            if caplog.records:
                break
            threading.Event().wait(0.02)
    assert any('test-work' in record.getMessage() and record.exc_info for record in caplog.records)


def test_missing_records_use_the_store_wording():
    with pytest.raises(Missing, match='Run not found'):
        MemoryStore('runs', missing='Run not found').get('nothing')


# Report runs (Market Monitor and MFI). The conftest fixture `run_store` is the memory store they use.

@pytest.fixture
def clock(monkeypatch):
    now = [1_000.0]
    monkeypatch.setattr(report_runs, "clock", lambda: now[0])
    return now


@pytest.mark.parametrize("backend", [None, "memory", " MEMORY ", "unknown"])
def test_memory_is_the_default_and_an_explicit_memory_backend_wins(monkeypatch, backend):
    for key in ("RUNS_BACKEND", "RUNS_GCS_URI"):
        monkeypatch.delenv(key, raising=False)
    if backend is not None:
        monkeypatch.setenv("RUNS_BACKEND", backend)
        # An explicit non-durable backend must take precedence over a leftover URI.
        monkeypatch.setenv("RUNS_GCS_URI", "gs://existing-runs/runs")
    assert isinstance(report_runs._open_store(), MemoryStore)


@pytest.mark.parametrize("backend", [None, "firestore_gcs", "firestore", "gcs", " FIRESTORE_GCS "])
def test_durable_store_is_selected_by_name_or_by_a_gcs_uri(monkeypatch, backend):
    for key in ("RUNS_BACKEND", "RUNS_FIRESTORE_DATABASE", "RUNS_FIRESTORE_COLLECTION"):
        monkeypatch.delenv(key, raising=False)
    if backend is not None:
        monkeypatch.setenv("RUNS_BACKEND", backend)
    monkeypatch.setenv("RUNS_GCS_URI", " gs://existing-runs/runs ")
    opened = {}
    monkeypatch.setattr(report_runs, "CloudStore", lambda **kwargs: opened.update(kwargs) or "cloud")
    assert report_runs._open_store() == "cloud"
    # The existing deployment settings keep their meaning: same database, collection, bucket and prefix.
    assert (opened["database"], opened["collection"], opened["bucket"], opened["prefix"]) == (
        "vam-llm-async", "async_runs", "existing-runs", "runs")


class FailingStore(MemoryStore):
    def mutate(self, run_id, fn):
        raise RuntimeError("firestore unavailable")


def test_durable_store_failure_stops_the_run_without_switching_to_memory(monkeypatch):
    monkeypatch.setattr(report_runs, "_STORE", FailingStore("runs"))
    with pytest.raises(report_runs.RunStoreUnavailable):
        report_runs.create_run("lost-run", service="market-monitor")

    # Durable storage named without a location: no store, and no memory fallback either.
    monkeypatch.setattr(report_runs, "_STORE", None)
    monkeypatch.setenv("RUNS_BACKEND", "firestore_gcs")
    monkeypatch.delenv("RUNS_GCS_URI", raising=False)
    with pytest.raises(report_runs.RunStoreUnavailable):
        report_runs.create_run("lost-run", service="market-monitor")
    assert report_runs._STORE is None


def _unavailable(_run_id, **_fields):
    raise report_runs.RunStoreUnavailable("Run storage is unavailable; the run was not started.")


_MOCK_REPORT_REQUEST = {
    "country": "South Sudan", "time_period": "2025-01", "commodity_list": ["Maize"],
    "admin1_list": [], "currency_code": "SSP", "enabled_modules": [], "use_mock_data": True,
}


def test_report_request_gets_503_when_run_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr(dispatcher, "create_run", _unavailable)
    monkeypatch.setattr(market_monitor_router, "create_run", _unavailable)

    local = dispatcher.dispatch_request("POST", "/market-monitor/generate-async", json_body=_MOCK_REPORT_REQUEST)
    api = TestClient(main.app).post("/market-monitor/generate-async", json=_MOCK_REPORT_REQUEST)

    assert local.status_code == api.status_code == 503
    assert "Run storage is unavailable" in local.json()["detail"]


def test_a_run_completes_in_one_write_and_its_result_loads_only_when_read(run_store):
    report_runs.create_run("r1", service="market-monitor")
    report_runs.update_run("r1", status="running", error=None, traceback=None)
    report_runs.update_run_progress("r1", current_node="data_agent", progress_pct=10)
    writes, reads = [], []
    mutate, read = run_store.mutate, run_store.read
    run_store.mutate = lambda run_id, fn: writes.append(run_id) or mutate(run_id, fn)
    run_store.read = lambda ref: reads.append(ref) or read(ref)

    report_runs.set_run_completed("r1", result={"country": "Testland", "pair": (1, 2)}, warnings=["check"],
                                  metadata={"qa_review": {"status": "passed"}})
    assert writes == ["r1"]

    run = report_runs.get_run("r1")
    assert (run.status, run.current_node, run.progress_pct, run.service) == ("completed", "END", 100, "market-monitor")
    assert (run.warnings, run.metadata) == (["check"], {"qa_review": {"status": "passed"}})
    assert reads == []  # status polls never pay for the result
    assert run.result == {"country": "Testland", "pair": [1, 2]} and len(reads) == 1


def test_a_silent_run_reads_as_interrupted_and_its_late_work_cannot_write(clock):
    report_runs.create_run("r1", service="mfi-drafter", silence_seconds=60)
    report_runs.update_run("r1", status="running")
    clock[0] += 59
    report_runs.update_run_progress("r1", current_node="draft_dimensions", progress_pct=40)  # moves the deadline on
    clock[0] += 59
    assert report_runs.get_run("r1").status == "running"

    clock[0] += 2
    run = report_runs.get_run("r1")
    assert (run.status, run.current_node, run.progress_pct) == ("interrupted", "draft_dimensions", 40)
    assert run.error == report_runs.INTERRUPTED

    report_runs.update_run("r1", current_node="late")  # ignored
    with pytest.raises(Superseded):
        report_runs.set_run_completed("r1", result={"late": True})
    report_runs.set_run_failed("r1", error="late failure")  # ignored: the run keeps its end state
    run = report_runs.get_run("r1")
    assert (run.status, run.current_node, run.error) == ("interrupted", "draft_dimensions", report_runs.INTERRUPTED)


def test_work_past_its_deadline_cannot_write_even_before_anyone_reads_the_run(clock):
    report_runs.create_run("r1", service="market-monitor", silence_seconds=60)
    clock[0] += 61
    with pytest.raises(Superseded):
        report_runs.set_run_completed("r1", result={})
    assert report_runs.get_run("r1").status == "interrupted"


def test_a_finished_run_keeps_its_end_state():
    report_runs.create_run("r1", service="market-monitor")
    report_runs.set_run_failed("r1", error="data gate", current_node="data_agent")
    report_runs.update_run("r1", status="running", current_node="late")
    with pytest.raises(Superseded):
        report_runs.set_run_completed("r1", result={})
    run = report_runs.get_run("r1")
    assert (run.status, run.error, run.current_node) == ("failed", "data gate", "data_agent")


def test_live_outputs_merge_section_by_section_and_values_are_stored_as_plain_json():
    report_runs.create_run("r1", service="market-monitor")
    report_runs.update_run("r1", metadata={"news_counts": {"seerist": 2}, "when": datetime(2026, 9, 26)},
                           live_outputs={"seerist": {"count": 2}}, warnings=["a", "", None])
    report_runs.update_run("r1", live_outputs={"reliefweb": {"count": (1,)}})
    run = report_runs.get_run("r1")
    assert run.metadata == {"news_counts": {"seerist": 2}, "when": "2026-09-26T00:00:00",
                            "live_outputs": {"seerist": {"count": 2}, "reliefweb": {"count": [1]}}}
    assert run.warnings == ["a"]


def test_add_and_get_run_artifact_and_a_new_artifact_replaces_its_namesake():
    report_runs.create_run("memory-run", service="market-monitor")
    for content in (b'{"ok": false}', b'{"ok": true}'):
        descriptor = report_runs.add_run_artifact(
            "memory-run", artifact_id="artifact_memory", label="Preview JSON", mime_type="application/json",
            file_name="preview.json", download_path="/service/artifacts/memory-run/artifact_memory", content=content)
    run = report_runs.get_run("memory-run")
    assert [item.artifact_id for item in run.artifacts] == ["artifact_memory"]
    assert descriptor["download_path"] == "/service/artifacts/memory-run/artifact_memory"
    artifact = report_runs.get_run_artifact("memory-run", "artifact_memory")
    assert (artifact.mime_type, artifact.content) == ("application/json", b'{"ok": true}')
    assert report_runs.get_run_artifact("memory-run", "missing") is None
    with pytest.raises(KeyError):
        report_runs.add_run_artifact("no-such-run", label="x", mime_type="text/plain", file_name="x.txt",
                                     download_path="/x", content="x")


def test_records_of_the_previous_store_stay_readable(run_store, monkeypatch, clock):
    objects = {"gs://old-runs/runs/legacy-gcs/result.json": b'{"country": "Old"}',
               "gs://old-runs/runs/legacy-gcs/artifacts/a1/rows.csv": b"a,b"}
    monkeypatch.setattr(report_runs, "read_gcs_uri", lambda uri: objects[uri])
    common = {"current_node": "END", "progress_pct": 100, "warnings": ["old"], "metadata": {"language": "fr"},
              "error": None, "traceback": None, "created_at": 900.0, "updated_at": 950.0}
    artifact = {"artifact_id": "a1", "label": "Rows", "mime_type": "text/csv", "file_name": "rows.csv",
                "download_path": "/market-monitor/artifacts/legacy/a1"}
    run_store.runs.update({
        "legacy-gcs": {**common, "status": "completed", "result": None,
                       "result_gcs_uri": "gs://old-runs/runs/legacy-gcs/result.json",
                       "artifacts": [{**artifact, "storage_uri": "gs://old-runs/runs/legacy-gcs/artifacts/a1/rows.csv"}]},
        "legacy-inline": {**common, "status": "completed", "result": {"country": "Inline"}, "result_gcs_uri": None,
                          "artifacts": [{**artifact, "inline_content_b64": base64.b64encode(b"x,y").decode()}]},
        "legacy-running": {**common, "status": "running", "current_node": "news_retrieval", "artifacts": []},
    })

    old = report_runs.get_run("legacy-gcs")
    assert (old.status, old.warnings, old.metadata, old.result) == ("completed", ["old"], {"language": "fr"}, {"country": "Old"})
    assert report_runs.get_run_artifact("legacy-gcs", "a1").content == b"a,b"
    assert report_runs.get_run("legacy-inline").result == {"country": "Inline"}
    assert report_runs.get_run_artifact("legacy-inline", "a1").content == b"x,y"

    # A run the previous store left "running" forever reads as interrupted once its silence is long enough.
    assert report_runs.get_run("legacy-running").status == "running"
    clock[0] = 950.0 + report_runs.DEFAULT_SILENCE_SECONDS
    assert report_runs.get_run("legacy-running").status == "interrupted"
