import json
from importlib import import_module

from app.shared.retrievers import SeeristRetriever


class FakeResponse:
    def __init__(self, payload, status_code=200, text="", url="https://seerist.test/api"):
        self._payload = payload
        self.status_code = status_code
        self.url = url
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, *, gets=None):
        self.gets = []
        self._get_responses = list(gets or [])

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        return self._get_responses.pop(0)


class FakeReliefWebRetriever:
    def __init__(self, verbose=False):
        self.verbose = verbose
        self.last_trace = {}

    @classmethod
    def build_economy_query(cls, extra_terms=None):
        return "reliefweb-query"

    def fetch(self, country, start_date, end_date, max_records=10, query=None):
        self.last_trace = {
            "retriever": "ReliefWeb",
            "country": country,
            "query": query,
            "num_documents": 1,
        }
        return [
            {
                "doc_id": "rw_1",
                "title": "ReliefWeb update",
                "url": "https://reliefweb.test/report-1",
                "source": "ReliefWeb",
                "date": start_date,
                "content": "ReliefWeb content",
            }
        ]


class FakeSeeristRetriever:
    DEFAULT_ECON_TERMS = ("market", "prices")

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.last_trace = {}

    @classmethod
    def build_lucene_or_query(cls, terms):
        return " OR ".join(str(term) for term in terms)

    def fetch_batch(self, *, queries, start_date, end_date, country, max_per_query=20):
        self.last_trace = {
            "retriever": "Seerist",
            "country": country,
            "queries": list(queries),
            "num_documents": 3,
            "error": None,
        }
        return [
            {
                "doc_id": "seerist_1",
                "title": "Seerist title 1",
                "url": "",
                "source": "Seerist",
                "date": end_date,
                "content": "",
            },
            {
                "doc_id": "seerist_1",
                "title": "Seerist title 1 duplicate",
                "url": "",
                "source": "Seerist",
                "date": end_date,
                "content": "duplicate content",
            },
            {
                "doc_id": "seerist_2",
                "title": "Seerist title 2",
                "url": "",
                "source": "Seerist",
                "date": end_date,
                "content": "Seerist content 2",
            },
        ]


def import_graph_module(module_name):
    return import_module(module_name)


def test_seerist_fetch_maps_features_to_documents():
    session = FakeSession(
        gets=[
            FakeResponse(
                {
                    "metadata": {"total": 1},
                    "features": [
                        {
                            "properties": {
                                "id": "123",
                                "title": {"en": "Market update"},
                                "sanitizedBody": {"en": "Body text from Seerist"},
                                "publishedDate": "2026-01-19T10:00:00.000Z",
                            }
                        }
                    ],
                }
            )
        ]
    )
    retriever = SeeristRetriever(api_key="test-key", session=session)

    documents = retriever.fetch(
        search_query="market",
        start_date="2026-01-01",
        end_date="2026-01-31",
        country="South Sudan",
        max_records=5,
    )

    assert documents == [
        {
            "doc_id": "seerist_123",
            "title": "Market update",
            "url": "",
            "source": "Seerist",
            "date": "2026-01-19",
            "content": "Body text from Seerist",
        }
    ]
    assert session.gets[0]["headers"]["x-api-key"] == "test-key"
    assert session.gets[0]["params"]["aoiId"] == "SS"
    assert session.gets[0]["params"]["search"] == "market"
    assert retriever.last_trace["retriever"] == "Seerist"
    assert retriever.last_trace["num_documents"] == 1
    assert retriever.last_trace["query_traces"][0]["total_available"] == 1


def test_seerist_normalizes_country_and_skips_unmapped_without_call():
    session = FakeSession(
        gets=[
            FakeResponse(
                {
                    "metadata": {"total": 0},
                    "features": [],
                }
            )
        ]
    )
    retriever = SeeristRetriever(api_key="test-key", session=session)

    retriever.fetch(
        search_query="inflation",
        start_date="2026-01-01",
        end_date="2026-01-31",
        country="southsudan",
        max_records=3,
    )

    assert retriever.last_trace["canonical_country"] == "South Sudan"
    assert retriever.last_trace["country_iso3"] == "SSD"
    assert retriever.last_trace["aoi_id"] == "SS"
    assert session.gets[0]["params"]["aoiId"] == "SS"

    unmapped_session = FakeSession()
    unmapped_retriever = SeeristRetriever(api_key="test-key", session=unmapped_session)
    documents = unmapped_retriever.fetch_batch(
        queries=["inflation"],
        start_date="2026-01-01",
        end_date="2026-01-31",
        country="West Bank",
        max_per_query=3,
    )

    assert documents == []
    assert "does not have a Seerist aoiId mapping" in unmapped_retriever.last_trace["error"]
    assert unmapped_session.gets == []


def test_seerist_gaza_uses_palestine_aoi_without_changing_search():
    for country in ("Gaza", "Gaza Strip", "  gAzA  "):
        session = FakeSession(
            gets=[FakeResponse({"metadata": {"total": 0}, "features": []})]
        )
        retriever = SeeristRetriever(api_key="test-key", session=session)

        documents = retriever.fetch_batch(
            queries=["market availability"],
            start_date="2026-01-01",
            end_date="2026-01-31",
            country=country,
            max_per_query=3,
        )

        assert documents == []
        assert len(session.gets) == 1
        assert session.gets[0]["params"]["aoiId"] == "PS"
        assert session.gets[0]["params"]["search"] == "market availability"
        assert retriever.last_trace["canonical_country"] == "Gaza Strip"
        assert retriever.last_trace["country_iso3"] == "PSG"
        assert retriever.last_trace["seerist_query_country"] == "Palestine, State of"
        assert retriever.last_trace["seerist_query_iso3"] == "PSE"
        assert retriever.last_trace["country_override"] == "gaza_to_palestine_aoi"
        assert retriever.last_trace["error"] is None


def test_seerist_palestine_uses_ps_without_gaza_override():
    session = FakeSession(
        gets=[FakeResponse({"metadata": {"total": 0}, "features": []})]
    )
    retriever = SeeristRetriever(api_key="test-key", session=session)

    retriever.fetch(
        search_query="prices",
        start_date="2026-01-01",
        end_date="2026-01-31",
        country="Palestine",
        max_records=3,
    )

    assert session.gets[0]["params"]["aoiId"] == "PS"
    assert retriever.last_trace["canonical_country"] == "Palestine, State of"
    assert retriever.last_trace["country_iso3"] == "PSE"
    assert retriever.last_trace["seerist_query_country"] == "Palestine, State of"
    assert retriever.last_trace["country_override"] is None


def test_mfi_and_market_monitor_share_the_gaza_aware_seerist_retriever():
    mfi_context = import_graph_module("app.services.mfi_drafter.context")
    market_graph = import_graph_module("app.services.market_monitor.graph")

    assert mfi_context.SeeristRetriever is SeeristRetriever
    assert market_graph.SeeristRetriever is SeeristRetriever


def test_market_monitor_news_retrieval_combines_and_deduplicates(monkeypatch):
    market_graph = import_graph_module("app.services.market_monitor.graph")
    monkeypatch.setattr(market_graph, "ReliefWebRetriever", FakeReliefWebRetriever)
    monkeypatch.setattr(market_graph, "SeeristRetriever", FakeSeeristRetriever)

    state = market_graph.create_initial_state(
        country="South Sudan",
        time_period="2025-01",
        commodity_list=[],
        admin1_list=[],
        currency_code="SSP",
        enabled_modules=[],
    )
    result = market_graph.node_news_retrieval(state)

    assert result["news_counts"] == {"Seerist": 2, "ReliefWeb": 1, "total": 3}
    assert len(result["documents"]) == 3
    assert any(doc["doc_id"] == "seerist_1" and doc["content"] == "Seerist title 1" for doc in result["documents"])
    assert result["retriever_traces"][1]["retriever"] == "Seerist"
    assert "warnings" not in result
    assert next(doc for doc in result["documents"] if doc["source"] == "ReliefWeb")["date"] == "2024-12-01"
    assert all(doc["date"] == "2025-01-31" for doc in result["documents"] if doc["source"] == "Seerist")
