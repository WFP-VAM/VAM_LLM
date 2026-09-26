"""Following a report run on a page: start_run and follow_run, as the Price Bulletin and MFI pages use them.

AppTest runs the polling fragment once per page run, so each app.run() below is one refresh.
"""
from __future__ import annotations

import builtins

from streamlit.testing.v1 import AppTest

import streamlit_shared

PAGE = '''
import builtins
import streamlit as st
import streamlit_shared as shared

probe = builtins._run_following_probe
if st.button("Start"):
    shared.start_run("t", "/start", json_body={"country": "Testland"})
shared.follow_run(
    "t",
    status_path="/status/{run_id}",
    result_path="/result/{run_id}",
    on_end=probe.ended,
    render_details=probe.details,
)
'''


class Probe:
    """The backend and the page's hooks. Status requests get `statuses` in turn; the last one repeats."""

    def __init__(self, statuses, result=None):
        self.statuses = list(statuses)
        self.result = result
        self.requests = []
        self.ends = []
        self.details_seen = []
        self.renders = []

    def request_json(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs.get("json_body")))
        if path == "/start":
            return {"run_id": "r1"}
        if path.startswith("/status/"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if isinstance(status, Exception):
                raise status
            return status
        if path.startswith("/result/"):
            return self.result
        raise AssertionError(f"Unexpected request: {method} {path}")

    def paths(self, prefix):
        return [path for _method, path, _body in self.requests if path.startswith(prefix)]

    def ended(self, run_id, status, result):
        self.ends.append((run_id, status["status"], result))

    def details(self, status):
        self.details_seen.append(status["status"])


def _page(monkeypatch, probe, *, url_run=None):
    monkeypatch.setattr(builtins, "_run_following_probe", probe, raising=False)
    monkeypatch.setattr(streamlit_shared, "request_json", probe.request_json)
    render = streamlit_shared.render_run_status

    def recording_render(status, **kwargs):
        probe.renders.append((status.get("status"), kwargs.get("enable_downloads", True)))
        render(status, **kwargs)

    monkeypatch.setattr(streamlit_shared, "render_run_status", recording_render)
    app = AppTest.from_string(PAGE)
    if url_run:
        app.query_params["t_run"] = url_run
    return app.run(timeout=20)


def test_a_started_run_is_followed_until_it_ends_and_its_result_handed_over_once(monkeypatch):
    running = {"run_id": "r1", "status": "running", "progress_pct": 40}
    probe = Probe([running, running, {"run_id": "r1", "status": "completed"}], result={"report": "ok"})
    app = _page(monkeypatch, probe)
    assert probe.requests == []

    app = app.button[0].click().run(timeout=20)
    assert probe.requests[0] == ("POST", "/start", {"country": "Testland"})
    assert app.query_params["t_run"] == ["r1"]
    assert probe.renders == [("running", False)]  # no downloads before the run ends
    assert probe.details_seen == ["running"]
    assert probe.ends == []

    app = app.run(timeout=20)
    assert probe.renders == [("running", False)] * 2
    assert probe.ends == []

    app = app.run(timeout=20)  # it completed: the result is loaded and handed over, then the page runs again
    assert probe.ends == [("r1", "completed", {"report": "ok"})]
    assert probe.paths("/result/") == ["/result/r1"]
    assert probe.renders == [("running", False)] * 2  # a completed run leaves the page to its result

    app = app.run(timeout=20)
    assert len(probe.ends) == 1
    assert probe.paths("/status/") == ["/status/r1"] * 3


def test_a_failed_run_keeps_its_final_status_on_the_page(monkeypatch):
    probe = Probe([{"run_id": "r1", "status": "failed", "error": "Model call failed"}])
    app = _page(monkeypatch, probe)

    app = app.button[0].click().run(timeout=20)
    assert probe.ends == [("r1", "failed", None)]
    assert probe.paths("/result/") == []
    assert any("Model call failed" in error.value for error in app.error)
    assert probe.renders[-1] == ("failed", True)  # with its downloads
    assert probe.details_seen[-1] == "failed"

    app = app.run(timeout=20)
    assert any("Model call failed" in error.value for error in app.error)
    assert len(probe.ends) == 1


def test_the_run_named_in_the_url_is_picked_up_once(monkeypatch):
    probe = Probe([{"run_id": "r9", "status": "completed"}], result={"report": "r9"})
    app = _page(monkeypatch, probe, url_run="r9")
    assert probe.ends == [("r9", "completed", {"report": "r9"})]

    app = app.run(timeout=20)
    assert len(probe.ends) == 1
    assert probe.paths("/status/") == ["/status/r9"]


def test_a_failed_status_request_is_shown_and_the_next_refresh_tries_again(monkeypatch):
    probe = Probe([RuntimeError("backend timed out"), {"run_id": "r1", "status": "completed"}], result={})
    app = _page(monkeypatch, probe)

    app = app.button[0].click().run(timeout=20)
    assert any("backend timed out" in error.value for error in app.error)
    assert probe.ends == []

    app = app.run(timeout=20)
    assert probe.ends == [("r1", "completed", {})]


def test_a_new_run_replaces_the_previous_final_status(monkeypatch):
    probe = Probe([{"run_id": "r1", "status": "failed", "error": "first failure"},
                   {"run_id": "r1", "status": "running"}])
    app = _page(monkeypatch, probe)
    app = app.button[0].click().run(timeout=20)
    assert any("first failure" in error.value for error in app.error)

    app = app.button[0].click().run(timeout=20)
    assert not [error for error in app.error if "first failure" in error.value]
    assert probe.renders[-1] == ("running", False)
