import pytest

from app.shared import async_runs


@pytest.mark.parametrize("backend", [None, "memory", " MEMORY "])
def test_memory_is_the_default_and_an_explicit_memory_backend_wins(monkeypatch, backend):
    for key in ("RUNS_BACKEND", "RUNS_GCS_URI"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(async_runs, "_BACKEND", None)
    if backend is not None:
        monkeypatch.setenv("RUNS_BACKEND", backend)
        # An explicit memory backend must take precedence over a leftover URI.
        monkeypatch.setenv("RUNS_GCS_URI", "gs://existing-runs/runs")
    assert not async_runs._use_durable_store()


@pytest.mark.parametrize("backend", [None, "firestore_gcs", "firestore", "gcs", " FIRESTORE_GCS "])
def test_durable_backend_is_selected_by_name_or_by_a_gcs_uri(monkeypatch, backend):
    monkeypatch.delenv("RUNS_BACKEND", raising=False)
    monkeypatch.setattr(async_runs, "_BACKEND", None)
    if backend is not None:
        monkeypatch.setenv("RUNS_BACKEND", backend)
    monkeypatch.setenv("RUNS_GCS_URI", " gs://existing-runs/runs ")
    monkeypatch.setattr(async_runs, "_has_gcp_deps", lambda: True)
    assert async_runs._use_durable_store()


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data or {})


class _FakeDocRef:
    def __init__(self, store, run_id):
        self._store = store
        self._run_id = run_id

    def get(self):
        return _FakeSnapshot(self._store.get(self._run_id))

    def set(self, payload, merge=True):
        current = dict(self._store.get(self._run_id) or {})
        if merge:
            current.update(payload)
            self._store[self._run_id] = current
        else:
            self._store[self._run_id] = dict(payload)


def _reset_memory_state():
    async_runs._RUNS.clear()
    async_runs._RUN_ARTIFACTS.clear()


def test_add_and_get_run_artifact_in_memory(monkeypatch):
    monkeypatch.setattr(async_runs, "_BACKEND", "memory")
    _reset_memory_state()

    async_runs.create_run("memory-run")
    descriptor = async_runs.add_run_artifact(
        "memory-run",
        artifact_id="artifact_memory",
        label="Preview JSON",
        mime_type="application/json",
        file_name="preview.json",
        download_path="/service/artifacts/memory-run/artifact_memory",
        content=b'{"ok": true}',
    )

    run = async_runs.get_run("memory-run")
    assert run is not None
    assert [item.artifact_id for item in run.artifacts] == ["artifact_memory"]
    assert descriptor["download_path"] == "/service/artifacts/memory-run/artifact_memory"

    artifact = async_runs.get_run_artifact("memory-run", "artifact_memory")
    assert artifact is not None
    assert artifact.mime_type == "application/json"
    assert artifact.content == b'{"ok": true}'


def test_add_and_get_run_artifact_with_durable_backend(monkeypatch):
    store = {}
    artifact_payloads = {}

    monkeypatch.setattr(async_runs, "_BACKEND", "firestore_gcs")
    monkeypatch.setattr(async_runs, "_RUNS", {})
    monkeypatch.setattr(async_runs, "_RUN_ARTIFACTS", {})
    monkeypatch.setattr(async_runs, "_firestore_doc_ref", lambda run_id: _FakeDocRef(store, run_id))

    def fake_upload(run_id, artifact_id, file_name, content, *, mime_type):
        uri = f"gs://test-bucket/{run_id}/{artifact_id}/{file_name}"
        artifact_payloads[uri] = bytes(content)
        return uri

    monkeypatch.setattr(async_runs, "_upload_run_artifact", fake_upload)
    monkeypatch.setattr(async_runs, "_download_artifact_bytes", lambda uri: artifact_payloads.get(uri))

    async_runs.create_run("durable-run")
    descriptor = async_runs.add_run_artifact(
        "durable-run",
        artifact_id="artifact_durable",
        label="Preview TXT",
        mime_type="text/plain",
        file_name="preview.txt",
        download_path="/service/artifacts/durable-run/artifact_durable",
        content="hello durable world",
    )

    run = async_runs.get_run("durable-run")
    assert run is not None
    assert [item.artifact_id for item in run.artifacts] == ["artifact_durable"]
    assert descriptor["file_name"] == "preview.txt"

    artifact = async_runs.get_run_artifact("durable-run", "artifact_durable")
    assert artifact is not None
    assert artifact.file_name == "preview.txt"
    assert artifact.content == b"hello durable world"
