"""Fixtures shared by several test modules."""
import json

import httpx
import pytest


@pytest.fixture
def vertex_wire(monkeypatch):
    """google-genai against a local HTTP transport: every request as sent, no network.

    Replies queued in `replies` are returned in order; after them, every request gets a short text reply.
    """
    from google import genai
    from google.oauth2.credentials import Credentials

    calls, replies = [], []
    real_client = genai.Client

    def transport(request):
        calls.append({"url": str(request.url), "body": json.loads(request.content), "headers": request.headers,
                      "timeout": (request.extensions.get("timeout") or {}).get("read")})
        if replies:
            return replies.pop(0)
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1, "totalTokenCount": 4}})

    def factory(**kwargs):
        options = kwargs.pop("http_options")
        options.httpx_client = httpx.Client(transport=httpx.MockTransport(transport))
        return real_client(credentials=Credentials(token="offline-test-token"), http_options=options, **kwargs)

    monkeypatch.setattr(genai, "Client", factory)
    return calls, replies
