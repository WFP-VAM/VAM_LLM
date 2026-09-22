import os

import pytest
import requests

import app.shared.databridges as databridges
from app.shared.databridges import DataBridgesAuth, DataBridgesClient


# The client id must be the resolved Azure application UUID (validated since the
# MarketAIssist hardening was ported in). Secrets stay opaque strings.
CLIENT_ID = "00000000-0000-4000-8000-000000000001"
OTHER_CLIENT_ID = "00000000-0000-4000-8000-000000000002"


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self._payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, *, posts=None, gets=None):
        self.posts = []
        self.gets = []
        self.headers = {}
        self._post_responses = list(posts or [])
        self._get_responses = list(gets or [])

    def post(self, url, **kwargs):
        self.posts.append({"url": url, **kwargs})
        response = self._post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        self.gets.append({"url": url, **kwargs})
        response = self._get_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class StubAuth:
    def __init__(self):
        self.scopes = []

    def get_token(self, scopes):
        self.scopes.append(tuple(scopes))
        return "token"


def test_token_exchange_uses_azure_payload_and_scope_cache():
    session = FakeSession(
        posts=[
            FakeResponse(
                {
                    "access_token": "abc",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            )
        ]
    )
    auth = DataBridgesAuth(
        CLIENT_ID,
        "client-secret",
        token_url="https://login.test/token",
        session=session,
    )

    assert auth.get_token(["scope-b", "scope-a"]) == "abc"
    assert auth.get_token(["scope-a", "scope-b"]) == "abc"
    assert len(session.posts) == 1
    assert session.posts[0]["url"] == "https://login.test/token"
    assert "auth" not in session.posts[0]
    assert session.posts[0]["data"] == {
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": "client-secret",
        "scope": "scope-a scope-b",
    }


def test_token_exchange_reports_sanitized_http_error():
    session = FakeSession(
        posts=[
            FakeResponse(
                {
                    "error": "invalid_grant",
                    "error_description": "Application is not assigned to a role.",
                    "access_token": "should-not-leak",
                },
                status_code=400,
            )
        ]
    )
    auth = DataBridgesAuth(CLIENT_ID, "client-secret", max_retries=1, session=session)

    with pytest.raises(RuntimeError) as exc_info:
        auth.get_token(["scope"])

    message = str(exc_info.value)
    assert "HTTP 400" in message
    assert "invalid_grant" in message
    assert "Application is not assigned to a role." in message
    assert "should-not-leak" not in message
    assert "client-secret" not in message


def test_token_exchange_reports_missing_access_token_safely():
    session = FakeSession(
        posts=[
            FakeResponse(
                {
                    "error": "invalid_client",
                    "error_description": "Bad credential.",
                    "refresh_token": "should-not-leak",
                }
            )
        ]
    )
    auth = DataBridgesAuth(CLIENT_ID, "client-secret", session=session)

    with pytest.raises(RuntimeError) as exc_info:
        auth.get_token(["scope"])

    message = str(exc_info.value)
    assert "did not include access_token" in message
    assert "invalid_client" in message
    assert "Bad credential." in message
    assert "should-not-leak" not in message


def test_pagination_accepts_items_items_capitalized_and_direct_lists():
    auth = StubAuth()
    session = FakeSession(
        gets=[
            FakeResponse({"Items": [{"id": 1}], "totalItems": 3}),
            FakeResponse({"items": [{"id": 2}], "totalItems": 3}),
            FakeResponse([{"id": 3}]),
            FakeResponse([]),
        ]
    )
    client = DataBridgesClient(
        CLIENT_ID,
        "secret",
        base_url="https://gateway.test",
        session=session,
        auth_provider=auth,
        scope="api-scope",
    )

    rows = client.list_commodities("SSD")

    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [call["params"]["page"] for call in session.gets] == [1, 2, 3]
    assert [call["params"]["env"] for call in session.gets] == ["prod", "prod", "prod"]
    assert auth.scopes == [("api-scope",), ("api-scope",), ("api-scope",)]


def test_pagination_uses_first_total_when_later_totals_drift():
    auth = StubAuth()
    session = FakeSession(
        gets=[
            FakeResponse({"items": [{"id": 1}], "totalItems": 3}),
            FakeResponse({"items": [{"id": 2}], "totalItems": 2}),
            FakeResponse({"items": [{"id": 3}], "totalItems": 2}),
        ]
    )
    client = DataBridgesClient(
        CLIENT_ID,
        "secret",
        base_url="https://example.test",
        session=session,
        auth_provider=auth,
        scope="api-scope",
    )

    rows = client.list_commodities("SSD")

    assert rows == [{"id": 1}, {"id": 2}, {"id": 3}]
    assert [call["params"]["page"] for call in session.gets] == [1, 2, 3]


def test_monthly_prices_use_v2_params_and_gateway_url():
    auth = StubAuth()
    session = FakeSession(gets=[FakeResponse({"items": [{"row": 1}], "totalItems": 1})])
    client = DataBridgesClient(
        CLIENT_ID,
        "secret",
        base_url="https://gateway.api.wfp.org/vam-data-bridges/v2",
        session=session,
        auth_provider=auth,
        scope="api-scope",
    )

    rows = client.list_monthly_prices(
        "SSD",
        commodity_id=52,
        start_date="2025-01-01",
        end_date="2025-12-31",
        latest_value_only=True,
        price_flag="actual",
        price_type_name="Retail",
    )

    assert rows == [{"row": 1}]
    call = session.gets[0]
    assert call["url"] == "https://gateway.api.wfp.org/vam-data-bridges/v2/MarketPrices/PriceMonthly"
    assert call["params"] == {
        "countryCode": "SSD",
        "latestValueOnly": "true",
        "format": "json",
        "commodityId": 52,
        "startDate": "2025-01-01",
        "endDate": "2025-12-31",
        "priceFlag": "actual",
        "priceTypeName": "Retail",
        "page": 1,
        "env": "prod",
    }


def test_mfi_processed_pagination_stops_on_short_page():
    auth = StubAuth()
    session = FakeSession(
        gets=[
            FakeResponse({"items": [{"row": 1}, {"row": 2}]}),
            FakeResponse({"items": [{"row": 3}]}),
        ]
    )
    client = DataBridgesClient(
        CLIENT_ID,
        "secret",
        base_url="https://example.test",
        session=session,
        auth_provider=auth,
        scope="api-scope",
    )

    rows = client.list_mfi_processed_data(123, page_size=2)

    assert rows == [{"row": 1}, {"row": 2}, {"row": 3}]
    assert session.gets[0]["params"]["pageSize"] == 2
    assert [call["params"]["page"] for call in session.gets] == [1, 2]


def test_timeout_and_http_errors_are_actionable():
    timeout_client = DataBridgesClient(
        CLIENT_ID,
        "secret",
        base_url="https://example.test",
        session=FakeSession(gets=[requests.exceptions.Timeout("slow")]),
        auth_provider=StubAuth(),
        timeout=7,
    )
    with pytest.raises(TimeoutError) as timeout_exc:
        timeout_client.list_markets("SSD")
    assert "after 7s" in str(timeout_exc.value)

    http_client = DataBridgesClient(
        CLIENT_ID,
        "secret",
        base_url="https://example.test",
        session=FakeSession(gets=[FakeResponse({}, status_code=500, text="server failed")]),
        auth_provider=StubAuth(),
    )
    with pytest.raises(RuntimeError) as http_exc:
        http_client.list_markets("SSD")
    assert "HTTP 500" in str(http_exc.value)
    assert "server failed" in str(http_exc.value)


def test_get_client_prefers_wfp_v2_environment(monkeypatch):
    databridges.reset_databridges_client_for_tests()
    for name in (
        "WFP_V2_API_KEY",
        "WFP_V2_API_SECRET",
        "WFP_V2_API_BASE_URL",
        "WFP_V2_TOKEN_URL",
        "WFP_V2_API_SCOPE",
        "WFP_V2_API_ENV",
        "DATA_BRIDGES_KEY",
        "DATA_BRIDGES_SECRET",
        "DATA_BRIDGES_API_BASE_URL",
        "DATA_BRIDGES_TOKEN_URL",
        "DATA_BRIDGES_SCOPE",
        "DATA_BRIDGES_ENV",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("DATA_BRIDGES_KEY", OTHER_CLIENT_ID)
    monkeypatch.setenv("DATA_BRIDGES_SECRET", "old-secret")
    monkeypatch.setenv("DATA_BRIDGES_API_BASE_URL", "https://old-base")
    monkeypatch.setenv("DATA_BRIDGES_TOKEN_URL", "https://old-token")
    monkeypatch.setenv("DATA_BRIDGES_SCOPE", "old-scope")
    monkeypatch.setenv("DATA_BRIDGES_ENV", "dev")
    monkeypatch.setenv("WFP_V2_API_KEY", CLIENT_ID)
    monkeypatch.setenv("WFP_V2_API_SECRET", "new-secret")
    monkeypatch.setenv("WFP_V2_API_BASE_URL", "https://new-base")
    monkeypatch.setenv("WFP_V2_TOKEN_URL", "https://new-token")
    monkeypatch.setenv("WFP_V2_API_SCOPE", "new-scope")
    monkeypatch.setenv("WFP_V2_API_ENV", "prod")

    client = databridges.get_databridges_client()

    assert client.base_url == "https://new-base"
    assert client.env == "prod"
    assert client.scope == "new-scope"
    assert client.auth_provider.api_key == CLIENT_ID
    assert client.auth_provider.api_secret == "new-secret"
    assert client.auth_provider.token_url == "https://new-token"
    databridges.reset_databridges_client_for_tests()


def test_get_client_falls_back_to_data_bridges_environment(monkeypatch):
    databridges.reset_databridges_client_for_tests()
    for name in (
        "WFP_V2_API_KEY",
        "WFP_V2_API_SECRET",
        "WFP_V2_API_BASE_URL",
        "WFP_V2_TOKEN_URL",
        "WFP_V2_API_SCOPE",
        "WFP_V2_API_ENV",
    ):
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("DATA_BRIDGES_KEY", OTHER_CLIENT_ID)
    monkeypatch.setenv("DATA_BRIDGES_SECRET", "old-secret")
    monkeypatch.setenv("DATA_BRIDGES_API_BASE_URL", "https://old-base")
    monkeypatch.setenv("DATA_BRIDGES_TOKEN_URL", "https://old-token")
    monkeypatch.setenv("DATA_BRIDGES_SCOPE", "old-scope")
    monkeypatch.setenv("DATA_BRIDGES_ENV", "dev")

    client = databridges.get_databridges_client()

    assert client.base_url == "https://old-base"
    assert client.env == "dev"
    assert client.scope == "old-scope"
    assert client.auth_provider.api_key == OTHER_CLIENT_ID
    assert client.auth_provider.api_secret == "old-secret"
    assert client.auth_provider.token_url == "https://old-token"
    databridges.reset_databridges_client_for_tests()


@pytest.mark.parametrize(
    ("client_id", "client_secret", "message"),
    [
        (
            "vam-p4-wfp-v2-api-key:latest",
            "resolved-client-secret",
            "Secret Manager reference",
        ),
        (
            CLIENT_ID,
            "projects/example/secrets/wfp-secret/versions/latest",
            "Secret Manager reference",
        ),
        (
            "not-an-azure-client-uuid",
            "resolved-client-secret",
            "Azure application/client UUID",
        ),
    ],
)
def test_invalid_or_unresolved_credentials_fail_before_network(
    client_id,
    client_secret,
    message,
):
    session = FakeSession(posts=[])

    with pytest.raises(ValueError, match=message):
        DataBridgesAuth(
            client_id,
            client_secret,
            session=session,
            max_retries=1,
        )


def test_credentials_are_stripped_before_validation():
    auth = DataBridgesAuth(
        f"  {CLIENT_ID}  ",
        "  resolved-client-secret  ",
        session=FakeSession(posts=[]),
    )

    assert auth.api_key == CLIENT_ID
    assert auth.api_secret == "resolved-client-secret"


@pytest.mark.skipif(
    not (os.getenv("WFP_V2_API_KEY") and os.getenv("WFP_V2_API_SECRET")),
    reason="WFP v2 Databridges credentials are not configured",
)
def test_live_wfp_v2_gateway_smoke():
    client = DataBridgesClient(
        os.environ["WFP_V2_API_KEY"],
        os.environ["WFP_V2_API_SECRET"],
        timeout=30,
        max_retries=1,
    )

    commodities = client.list_commodities("SSD")
    markets = client.list_markets("SSD")
    commodity_id = commodities[0].get("id") or commodities[0].get("commodityId")
    latest_prices = client.list_monthly_prices(
        "SSD",
        commodity_id=int(commodity_id),
        latest_value_only=True,
    )

    assert commodities
    assert markets
    assert latest_prices
