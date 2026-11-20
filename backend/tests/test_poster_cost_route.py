from types import SimpleNamespace

from fastapi import BackgroundTasks

from app.routes import whole_image
from app.services import poster_costs
from app.services.scoped_db import ScopedDatabase
import mongomock


def test_workspace_cost_route_returns_summary_before_details():
    scoped = ScopedDatabase(mongomock.MongoClient().db, "merchant-one")
    with poster_costs.track(scoped, "design", "optimize"):
        call = poster_costs.begin("llm", "model-one")
        poster_costs.finish(call, {"usage": {"cost_usd": 0.04}}, status="completed")
    request = SimpleNamespace(state=SimpleNamespace(db=scoped))
    summary = whole_image.get_workspace_costs("design", request, BackgroundTasks())
    assert summary["total_usd"] == 0.04
    assert "calls" not in summary
    detail = whole_image.get_workspace_costs("design", request, BackgroundTasks(), details=True)
    assert detail["calls"][0]["operation"] == "optimize"
