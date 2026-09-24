"""Context retrieval for the live MFI workflow: ReliefWeb and Seerist documents, no model calls."""
from app.services.mfi_drafter import context, light_graph
from app.shared.retrievers import ReliefWebRetriever, SeeristRetriever

STATE = {"country": "South Sudan", "data_collection_start": "2025-01-01", "data_collection_end": "2025-01-31"}


class FakeReliefWeb:
    def __init__(self, verbose=False):
        self.last_trace = {}

    @classmethod
    def build_economy_query(cls, extra_terms=None):
        return "reliefweb-query"

    def fetch(self, country, start_date, end_date, max_records=10, query=None):
        self.last_trace = {"retriever": "ReliefWeb", "country": country, "query": query, "num_documents": 1}
        return [{"doc_id": "rw_1", "title": "ReliefWeb update", "url": "https://reliefweb.test/report-1",
                 "source": "ReliefWeb", "date": start_date, "content": "ReliefWeb content"}]


class FakeSeerist:
    DEFAULT_ECON_TERMS = ("market", "prices")

    def __init__(self, verbose=False):
        self.last_trace = {}

    @classmethod
    def build_lucene_or_query(cls, terms):
        return " OR ".join(str(term) for term in terms)

    def fetch_batch(self, *, queries, start_date, end_date, country, max_per_query=20):
        self.last_trace = {"retriever": "Seerist", "country": country, "queries": list(queries), "error": None}
        return [
            {"doc_id": "seerist_1", "title": "Seerist title 1", "url": "", "source": "Seerist", "date": end_date, "content": ""},
            {"doc_id": "seerist_1", "title": "Duplicate", "url": "", "source": "Seerist", "date": end_date, "content": "duplicate"},
            {"doc_id": "seerist_2", "title": "Seerist title 2", "url": "", "source": "Seerist", "date": end_date, "content": "Seerist content 2"},
        ]


class UnavailableSeerist(FakeSeerist):
    def fetch_batch(self, *, queries, start_date, end_date, country, max_per_query=20):
        self.last_trace = {"retriever": "Seerist", "country": country, "error": "Missing SEERIST_API_KEY."}
        return []


class FailedReliefWeb(FakeReliefWeb):
    def fetch(self, country, start_date, end_date, max_records=10, query=None):
        self.last_trace = {"error": "provider secret detail"}
        return []


def test_context_uses_the_shared_retrievers():
    assert context.ReliefWebRetriever is ReliefWebRetriever
    assert context.SeeristRetriever is SeeristRetriever


def test_documents_are_merged_deduplicated_and_counted(monkeypatch):
    monkeypatch.setattr(context, "ReliefWebRetriever", FakeReliefWeb)
    monkeypatch.setattr(context, "SeeristRetriever", FakeSeerist)
    result = context.retrieve_context_documents(STATE)
    assert result["context_counts"] == {"Seerist": 2, "ReliefWeb": 1, "total": 3}
    assert [d["doc_id"] for d in result["contextual_documents"]] == ["rw_1", "seerist_1", "seerist_2"]
    assert result["contextual_documents"][1]["content"] == "Seerist title 1"
    assert result["document_references"][0] == {"doc_id": "rw_1", "source": "ReliefWeb", "title": "ReliefWeb update",
                                                 "url": "https://reliefweb.test/report-1", "date": "2025-01-01"}
    assert [t["retriever"] for t in result["retriever_traces"]] == ["ReliefWeb", "Seerist"]
    assert {name: r["status"] for name, r in result["context_status"]["retrievers"].items()} == {
        "ReliefWeb": "completed", "Seerist": "completed"}


def test_unavailable_seerist_falls_back_to_reliefweb(monkeypatch):
    monkeypatch.setattr(context, "ReliefWebRetriever", FakeReliefWeb)
    monkeypatch.setattr(context, "SeeristRetriever", UnavailableSeerist)
    result = context.retrieve_context_documents(STATE)
    assert result["context_counts"] == {"Seerist": 0, "ReliefWeb": 1, "total": 1}
    assert result["context_status"]["limitation_code"] == "context_partial_retrieval_unavailable"
    assert result["context_status"]["retrievers"]["Seerist"]["status"] == "failed"
    assert result["retriever_traces"][1]["error"] == "Missing SEERIST_API_KEY."


def test_raw_retriever_error_stays_in_the_trace(monkeypatch):
    monkeypatch.setattr(context, "ReliefWebRetriever", FailedReliefWeb)
    monkeypatch.setattr(context, "SeeristRetriever", FakeSeerist)
    result = context.retrieve_context_documents(STATE)
    assert result["context_status"]["status"] == "no_accepted_statements"
    assert result["context_status"]["limitation_code"] == "context_partial_retrieval_unavailable"
    assert result["retriever_traces"][0]["error"] == "provider secret detail"
    assert "warnings" not in result


def test_light_workflow_maps_documents_to_sources(monkeypatch):
    monkeypatch.setattr(context, "ReliefWebRetriever", FakeReliefWeb)
    monkeypatch.setattr(context, "SeeristRetriever", FakeSeerist)
    result = light_graph.retrieve_context(STATE)
    assert set(result["sources"]) == {"S1", "S2", "S3"}
    assert result["context_status"]["status"] == "available"
    assert result["context_status"]["total_documents"] == 3
    assert result["context_limitation"] is None
    assert all(ref["source_id"] in result["sources"] for ref in result["document_references"])


def test_light_workflow_keeps_context_optional_when_retrieval_fails(monkeypatch):
    def broken(state):
        raise RuntimeError("network down")
    monkeypatch.setattr(context, "retrieve_context_documents", broken)
    result = light_graph.retrieve_context(STATE)
    assert result["sources"] == {} and result["document_references"] == []
    assert result["retriever_traces"] == [{"retriever": "context", "error": "RuntimeError"}]
    assert result["context_limitation"].startswith("No usable external context")
