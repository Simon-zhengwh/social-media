"""Record new Core AI requests made on behalf of a poster workspace."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.time_utils import utc_iso

logger = logging.getLogger(__name__)
TERMINAL_RUNS = {"COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "SKIPPED"}


@dataclass(frozen=True)
class Scope:
    db: Any
    workspace_id: str
    operation: str
    image_number: int | None


_scope: ContextVar[Scope | None] = ContextVar("poster_cost_scope", default=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def track(db: Any, workspace_id: str | None, operation: str,
          *, image_number: int | None = None) -> Iterator[None]:
    token = _scope.set(Scope(db, workspace_id, operation, image_number) if workspace_id else None)
    try:
        yield
    finally:
        _scope.reset(token)


def track_task(db: Any, task: dict[str, Any], operation: str):
    input_data = task.get("input") or {}
    workspace = input_data.get("generation_workspace_id") or input_data.get("candidate_batch_id") or task.get("_id")
    series = task.get("series") or {}
    index = series.get("order") if series else input_data.get("candidate_index", 0)
    image_number = index + 1 if isinstance(index, int) and index >= 0 else None
    return track(db, workspace, operation, image_number=image_number)


def begin(kind: str, target: str) -> str | None:
    scope = _scope.get()
    if scope is None:
        return None
    call_id = "poster-cost-" + uuid4().hex
    now = _now()
    try:
        scope.db.whole_cost_calls.insert_one({
            "_id": call_id, "workspace_id": scope.workspace_id, "operation": scope.operation,
            "image_number": scope.image_number, "kind": kind, "target": target,
            "status": "running", "created_at": now, "cost_usd": None, "credits": None,
            "needs_sync": True, "next_check_at": now + timedelta(minutes=5),
        })
    except PyMongoError:
        logger.exception("Unable to record poster cost request")
        return None
    return call_id


def _amount(*values: Any) -> float | None:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        try:
            amount = Decimal(str(value))
            if amount.is_finite() and Decimal(0) <= amount <= Decimal("1e20"):
                return float(amount)
        except InvalidOperation:
            continue
    return None


def _total(rows: list[dict[str, Any]], field: str) -> float | None:
    amounts = [Decimal(str(row[field])) for row in rows if row.get(field) is not None]
    return float(sum(amounts)) if amounts else None


def finish(call_id: str | None, payload: dict[str, Any] | None = None, *,
           status: str, run_id: str | None = None, sync: bool | None = None) -> None:
    scope = _scope.get()
    if scope is None or call_id is None:
        return
    raw = payload or {}
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    cost = _amount(raw.get("cost_usd"), usage.get("cost_usd"), usage.get("upstream_cost_usd"))
    credits = _amount(raw.get("credits_consumed"), usage.get("credits_consumed"))
    changes: dict[str, Any] = {
        "status": status, "cost_usd": cost, "credits": credits,
        "needs_sync": bool(run_id) or (sync if sync is not None else cost is None),
        "next_check_at": _now(),
    }
    if run_id:
        changes["run_id"] = run_id
    if isinstance(raw.get("model"), str) and raw["model"]:
        changes["target"] = raw["model"]
    try:
        scope.db.whole_cost_calls.update_one({"_id": call_id}, {"$set": changes})
    except PyMongoError:
        logger.exception("Unable to update poster cost request %s", call_id)


def report(db: Any, workspace_id: str, *, details: bool = False) -> dict[str, Any]:
    projection = None if details else {"cost_usd": 1, "credits": 1, "needs_sync": 1, "status": 1, "partial": 1}
    rows = list(db.whole_cost_calls.find({"workspace_id": workspace_id}, projection).sort("created_at", 1))
    result: dict[str, Any] = {
        "total_usd": _total(rows, "cost_usd") if rows else 0,
        "total_credits": _total(rows, "credits") or 0,
        "call_count": len(rows),
        "pending_count": sum(bool(row["needs_sync"]) for row in rows),
        "unreported_count": sum(
            not row["needs_sync"] and (
                bool(row.get("partial")) or (
                    row.get("status") in {"unknown", "completed"} and row["cost_usd"] is None
                )
            ) for row in rows
        ),
    }
    if details:
        fields = ("kind", "operation", "target", "image_number", "status", "cost_usd", "credits")
        result["calls"] = [{
            "id": row["_id"], **{field: row.get(field) for field in fields},
            "started_at": utc_iso(row["created_at"]), "children": row.get("children") or [],
        } for row in rows]
    return result


def _trace_id(trace: dict[str, Any]) -> str:
    return str(trace.get("traceId") or trace.get("trace_id") or "")


def _span_entry(span: dict[str, Any], kind: str) -> dict[str, Any]:
    attributes = span.get("attributes") or {}
    credits = attributes.get("media.units") if attributes.get("media.unit_type") == "credit" else None
    return {
        "id": str(span.get("spanId") or span.get("id") or ""),
        "kind": kind,
        "target": (
            attributes.get("gen_ai.agent.name") if kind == "agent" else attributes.get("media.resolved_model")
        ) or span.get("model") or span.get("name") or "",
        "started_at": span.get("startedAt") or span.get("createdAt"),
        "cost_usd": _amount(span.get("costUsd"), attributes.get("gen_ai.usage.cost_usd")),
        "credits": _amount(span.get("creditsConsumed"), attributes.get("media.credits_consumed"), credits),
    }


def _nest_agent_details(rows: list[dict[str, Any]], spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    parents = {span.get("spanId"): span.get("parentSpanId") for span in spans if span.get("spanId")}
    agents = {row["id"]: row for row in rows if row["kind"] == "agent"}
    roots = []
    for row in rows:
        seen = {row["id"]}
        parent = parents.get(row["id"])
        nearest_agent = None
        while parent and parent not in seen:
            if nearest_agent is None and parent in agents:
                nearest_agent = agents[parent]
            seen.add(parent)
            parent = parents.get(parent)
        if nearest_agent is not None and not parent:
            nearest_agent.setdefault("children", []).append(row)
        else:
            roots.append(row)

    def subtotal(row: dict[str, Any]) -> None:
        children = row.get("children") or []
        if children:
            for child in children:
                subtotal(child)
            row["cost_usd"] = _amount(row["cost_usd"], _total(children, "cost_usd"))
            row["credits"] = _amount(row["credits"], _total(children, "credits"))

    for root in roots:
        subtotal(root)
    return roots


def _agent_charges(client: Any, run_id: str, detail: dict[str, Any]) -> dict[str, Any] | None:
    root_id = _trace_id(detail)
    traces = client.list_cost_traces("run:" + run_id)
    root = next((trace for trace in traces if _trace_id(trace) == root_id), None)
    if root is None and root_id:
        root = client.get_cost_trace(root_id)
    if root is None:
        root = next((trace for trace in traces if trace.get("type") == "agent"
                     and (trace.get("metadata") or {}).get("agent_run_id") == run_id), {})
        root_id = _trace_id(root)
    if not root and not traces:
        return None
    spans = client.get_agent_trace_spans(root_id) if root_id else []
    children = []
    for span in spans:
        kind = {"LLM": "llm", "MEDIA": "image"}.get(span.get("type"))
        if kind:
            children.append(_span_entry(span, kind))
        elif span.get("type") == "AGENT" and span.get("name") == "agent.run" and span.get("parentSpanId"):
            # Sub-agent rows can include their descendants. The root trace supplies
            # the authoritative amount that is added to the workspace total.
            children.append(_span_entry(span, "agent"))

    separate = []
    root_media_jobs = {
        (span.get("attributes") or {}).get("media.job_id")
        for span in spans if span.get("type") == "MEDIA"
    }
    for trace in traces:
        trace_id = _trace_id(trace)
        if trace_id == root_id:
            continue
        if trace.get("type") == "media":
            media = next((span for span in client.get_agent_trace_spans(trace_id) if span.get("type") == "MEDIA"), None)
            attributes = (media or {}).get("attributes") or {}
            job = attributes.get("media.job_id")
            if job:
                if job in root_media_jobs:
                    continue
                root_media_jobs.add(job)
            entry = _span_entry({**trace, **(media or {}), "id": trace_id}, "image")
            entry["cost_usd"] = _amount(entry["cost_usd"], trace.get("costUsd"))
        elif trace.get("type") in {"agent", "llm"}:
            kind = trace["type"]
            entry = _span_entry({**trace, "id": trace_id}, kind)
            if kind == "agent":
                entry["target"] = trace.get("agentName") or trace.get("name") or entry["target"]
        else:
            continue
        separate.append(entry)

    image_rows = sum(row["kind"] == "image" for row in children + separate)
    image_tools = [span for span in spans if span.get("type") == "TOOL" and span.get("name") == "generate_image"]
    children.extend(_span_entry(span, "image") for span in image_tools[image_rows:])
    root_cost = _amount(root.get("costUsd"))
    # The root cost already includes its LLM and any in-trace media spans.
    total_usd = _total([{"cost_usd": root_cost}, *separate], "cost_usd")
    all_rows = children + separate
    partial = root_cost is None or any(row["cost_usd"] is None for row in all_rows if row["kind"] == "image")
    children = _nest_agent_details(children, spans) + separate
    children.sort(key=lambda row: row["started_at"] or "")
    return {
        "cost_usd": total_usd, "credits": _total(children, "credits"), "children": children,
        "target": root.get("agentName") or root.get("name") or detail.get("agent_id"),
        "partial": partial,
    }


def _refresh_agent(client: Any, row: dict[str, Any], now: datetime) -> dict[str, Any]:
    run_id = row.get("run_id")
    if not run_id:
        return {"needs_sync": False}
    detail = client.get_agent_run(run_id)
    status = detail.get("status")
    if status not in TERMINAL_RUNS:
        return {}
    seen_at = row.get("terminal_seen_at")
    stable = bool(seen_at and seen_at.replace(tzinfo=timezone.utc) <= now - timedelta(seconds=30))
    charges = _agent_charges(client, run_id, detail)
    changes: dict[str, Any] = {
        "status": "completed" if status == "COMPLETED" else "failed",
        "terminal_seen_at": seen_at or now,
    }
    if charges is not None:
        if not charges["target"]:
            charges.pop("target")
        changes.update(charges)
        changes["needs_sync"] = not stable
    return changes


def _refresh_gateway(client: Any, row: dict[str, Any]) -> dict[str, Any]:
    traces = client.list_cost_traces(row["_id"])
    cost = _total([{"cost_usd": _amount(trace.get("costUsd"))} for trace in traces], "cost_usd")
    if cost is None:
        return {}
    credits = row.get("credits")
    if credits is None and row["kind"] == "image":
        media = [span for trace in traces if trace.get("type") == "media"
                 for span in client.get_agent_trace_spans(_trace_id(trace)) if span.get("type") == "MEDIA"]
        credits = _total([_span_entry(span, "image") for span in media], "credits")
    return {
        "cost_usd": cost, "credits": credits, "needs_sync": False,
        "status": "completed" if row["status"] == "running" else row["status"],
    }


def refresh_due(db: Any, workspace_id: str, client: Any) -> None:
    now = _now()
    lease_until = now + timedelta(minutes=2)
    for _ in range(4):
        row = db.whole_cost_calls.find_one_and_update(
            {"workspace_id": workspace_id, "needs_sync": True, "next_check_at": {"$lte": now}},
            {"$set": {"next_check_at": lease_until}},
            sort=[("next_check_at", 1)], return_document=ReturnDocument.AFTER,
        )
        if row is None:
            return
        try:
            if row["kind"] == "agent":
                changes = _refresh_agent(client, row, now)
            else:
                changes = _refresh_gateway(client, row)
        except Exception:
            logger.warning("Unable to refresh poster cost %s", row["_id"], exc_info=True)
            changes = {}
        if row["created_at"].replace(tzinfo=timezone.utc) <= now - timedelta(days=1):
            changes["needs_sync"] = False
            if changes.get("cost_usd", row["cost_usd"]) is None:
                changes["partial"] = True
                if row["status"] == "running" and "status" not in changes:
                    changes["status"] = "unknown"
        changes["next_check_at"] = _now() + timedelta(seconds=20)
        db.whole_cost_calls.update_one(
            {"_id": row["_id"], "next_check_at": row["next_check_at"], "needs_sync": True},
            {"$set": changes},
        )
