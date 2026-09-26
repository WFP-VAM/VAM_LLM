"""The Streamlit pages' requests, answered by the FastAPI application in this same process.

`dispatch_request` hands each request to `app.api.app` through httpx's ASGI transport, on an event loop of
its own. Nothing goes over the network, and every endpoint has one implementation, the FastAPI routers: the
pages get the API's own validation, errors and responses. Reports are launched by the routers on the shared
background launcher, so a request that starts one returns as soon as its run exists.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE_URL = "http://in-process"


class _QuietInProcessRequests(logging.Filter):
    """httpx logs every request at INFO; the pages polling this process are not worth a log line each."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not (len(args) > 1 and str(args[1]).startswith(_BASE_URL))


logging.getLogger("httpx").addFilter(_QuietInProcessRequests())


@dataclass
class LocalResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""

    @property
    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except Exception:
            return ""

    def json(self) -> Any:
        if not self.content:
            raise ValueError("No JSON content")
        return json.loads(self.content.decode("utf-8"))


def _form_fields(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Form fields as a browser sends them: text, and an unset field left out."""
    if not data:
        return None
    return {key: value if isinstance(value, str) else str(value) for key, value in data.items() if value is not None}


def dispatch_request(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Any = None,
    data: Optional[Dict[str, Any]] = None,
    files: Any = None,
) -> LocalResponse:
    """Answer a page's request with the FastAPI application, in this process. An unhandled error becomes a 500."""
    from app.api import app

    options: Dict[str, Any] = {"params": params or None, "data": _form_fields(data), "files": files or None}
    if json_body is not None:
        options["json"] = json_body

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=_BASE_URL) as client:
            return await client.request((method or "GET").upper(), path, **options)

    try:
        reply = asyncio.run(send())
    except Exception as exc:
        logger.exception("Local dispatcher error")
        return LocalResponse(status_code=500, headers={"content-type": "application/json"},
                             content=json.dumps({"detail": str(exc)}).encode("utf-8"))
    return LocalResponse(status_code=reply.status_code, headers=reply.headers, content=reply.content)
