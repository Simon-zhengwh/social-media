import base64

import httpx
import mongomock
import pytest

from app.services import poster_costs
from app.services.core_ai import CoreAIClient, CoreAIError
from app.services.scoped_db import ScopedDatabase


def test_image_gateway_records_settled_values_and_keeps_image_bytes(settings):
    raw = mongomock.MongoClient().db
    db = ScopedDatabase(raw, "merchant-one")

    def handler(request):
        assert request.url.path.endswith("/images/generations")
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(b"image").decode()}],
            "cost_usd": 0.08,
            "usage": {"upstream_cost_usd": 0.07, "credits_consumed": 2.5},
        })

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        client.generate_image("old", size="1024x1024")
        with poster_costs.track_task(db, {"_id": "img-1", "input": {"generation_workspace_id": "design"},
                                         "series": {"order": 1}}, "generate"):
            assert client.generate_image("new", size="1024x1024") == b"image"
    finally:
        client.close()

    summary = poster_costs.report(db, "design")
    assert summary == {"total_usd": 0.08, "total_credits": 2.5, "call_count": 1, "pending_count": 0, "unreported_count": 0}
    row, = poster_costs.report(db, "design", details=True)["calls"]
    assert (row["operation"], row["kind"], row["image_number"]) == ("generate", "image", 2)
    assert poster_costs.report(ScopedDatabase(raw, "merchant-two"), "design")["call_count"] == 0


def test_agent_cost_adds_separate_media_traces_and_shows_llm_details_once(settings):
    db = ScopedDatabase(mongomock.MongoClient().db, "merchant-one")

    def handler(request):
        path = request.url.path
        if request.method == "POST":
            return httpx.Response(202, json={"run_id": "new-run", "status": "PENDING"})
        if path == "/api/runs/new-run":
            return httpx.Response(200, json={"trace_id": "agent-trace", "status": "FAILED"})
        if path == "/api/traces":
            assert request.url.params["sessionId"] == "run:new-run"
            image = {"traceId": "image-trace", "sessionId": "run:new-run", "type": "media",
                     "name": "Image generation", "model": "seedream", "costUsd": 0.14}
            return httpx.Response(200, json={"traces": [
                {"traceId": "agent-trace", "sessionId": "run:new-run", "type": "agent",
                 "agentName": "Image Editor", "costUsd": 0.12},
                image, image,
                {**image, "traceId": "other", "sessionId": "run:other", "costUsd": 300},
            ], "total": 4})
        if path == "/api/traces/agent-trace/spans":
            return httpx.Response(200, json={"spans": [
                {"spanId": "root", "type": "AGENT", "name": "agent.run", "costUsd": 0.12},
                {"spanId": "sub", "parentSpanId": "root", "type": "AGENT", "name": "agent.run",
                 "attributes": {"gen_ai.agent.name": "Planner"}},
                {"spanId": "turn", "parentSpanId": "sub", "type": "AGENT", "name": "agent.turn"},
                {"spanId": "llm", "parentSpanId": "turn", "type": "LLM", "model": "deepseek", "costUsd": 0.12},
                {"spanId": "tool", "type": "TOOL", "name": "generate_image"},
            ]})
        if path == "/api/traces/image-trace/spans":
            return httpx.Response(200, json={"spans": [
                {"spanId": "image", "type": "MEDIA", "model": "seedream", "costUsd": 0.14,
                 "attributes": {"media.unit_type": "credit", "media.units": "12"}},
            ]})
        raise AssertionError(path)

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        with poster_costs.track(db, "design", "replace"):
            assert client.trigger_agent("agent-id", "edit", timeout_seconds=5) == "new-run"
        assert poster_costs.report(db, "design")["pending_count"] == 1
        poster_costs.refresh_due(db, "design", client)
        poster_costs.refresh_due(db, "design", client)
    finally:
        client.close()

    report = poster_costs.report(db, "design", details=True)
    assert (report["total_usd"], report["total_credits"], report["call_count"]) == (0.26, 12, 1)
    row, = report["calls"]
    assert (row["target"], row["status"]) == ("Image Editor", "failed")
    assert {(item["kind"], item["target"], item["cost_usd"]) for item in row["children"]} == {
        ("agent", "Planner", 0.12), ("image", "seedream", 0.14),
    }
    sub_agent = next(item for item in row["children"] if item["kind"] == "agent")
    llm, = sub_agent["children"]
    assert (llm["kind"], llm["target"], llm["cost_usd"]) == ("llm", "deepseek", 0.12)



def test_unknown_network_result_is_visible_in_a_partial_total(settings):
    db = mongomock.MongoClient().db

    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client = CoreAIClient(settings, transport=httpx.MockTransport(handler))
    try:
        with poster_costs.track(db, "design", "generate"):
            paid = poster_costs.begin("image", "paid-model")
            poster_costs.finish(paid, {"cost_usd": 0.06}, status="completed")
            with pytest.raises(CoreAIError, match="结果未知"):
                client.generate_image("unknown", size="1024x1024")
    finally:
        client.close()

    summary = poster_costs.report(db, "design", details=True)
    assert summary["total_usd"] == 0.06
    assert summary["pending_count"] == 1
    unknown, = [row for row in summary["calls"] if row["status"] == "unknown"]
    db.whole_cost_calls.update_one({"_id": unknown["id"]}, {"$set": {"needs_sync": False}})
    assert poster_costs.report(db, "design")["unreported_count"] == 1
