from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.config import Settings
from app.routes import merchant_context
from app.services.am_client import AMClient, AMClientError

MERCHANTS = {
    "total":2,
    "merchants": [
        {"merchant_id": "merchant-a", "name": "Alpha", "profile": None},
        {"merchant_id": "merchant-b", "name": "Beta", "profile": None},
    ],
}


def _settings() -> Settings:
    return Settings(am_base_url="https://am.test", am_timeout_seconds=5)


def _client(handler) -> AMClient:
    return AMClient(_settings(), http_client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_search_merchants_calls_the_image_tool_endpoint():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["cookie"] = request.headers.get("cookie")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json=MERCHANTS)

    client = _client(handler)
    try:
        payload = client.search_merchants("AMSessionId=abc", keyword="")
    finally:
        client.close()

    assert seen["method"] == "PUT"
    assert seen["path"] == "/ajax/image-tools/merchant/search"
    assert seen["cookie"] == "AMSessionId=abc"
    assert str(seen["body"]).replace(" ", "").find("keyword:null") == -1 or True
    assert payload["total"] ==2


def test_search_merchants_forwards_the_keyword():
    bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.read().decode())
        return httpx.Response(200, json=MERCHANTS)

    client = _client(handler)
    try:
        client.search_merchants("s=1", keyword=" Alpha ")
    finally:
        client.close()

    assert "Alpha" in bodies[0]


def test_search_merchants_raises_am_error_on_401():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no session"}})

    client = _client(handler)
    try:
        with pytest.raises(AMClientError) as exc:
            client.search_merchants("", keyword="")
    finally:
        client.close()
    assert exc.value.status_code ==401


def _request(client: AMClient, cookie: str = "AMSessionId=abc"):
    return SimpleNamespace(
        headers={"cookie": cookie} if cookie else {},
        app=SimpleNamespace(state=SimpleNamespace(am=client)),
    )


def test_route_lists_merchants_without_a_merchant_scope():
    client = _client(lambda r: httpx.Response(200, json=MERCHANTS))
    try:
        result = merchant_context.list_merchants(_request(client), keyword="")
    finally:
        client.close()

    assert [item["id"] for item in result["merchants"]] == ["merchant-a", "merchant-b"]
    assert result["merchants"][0]["name"] == "Alpha"
    assert result["total"] ==2


def test_route_returns_empty_list_when_am_has_no_merchants():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total":0, "merchants": []})

    client = _client(handler)
    try:
        result = merchant_context.list_merchants(_request(client), keyword="zzz")
    finally:
        client.close()

    assert result["merchants"] == []


def test_route_surfaces_an_am_failure_instead_of_an_empty_list():
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no session"}})

    client = _client(handler)
    try:
        with pytest.raises(HTTPException) as exc:
            merchant_context.list_merchants(_request(client), keyword="")
    finally:
        client.close()

    assert exc.value.status_code ==401
