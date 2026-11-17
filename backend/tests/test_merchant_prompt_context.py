import json

import mongomock
import pytest
from types import SimpleNamespace

from app.schemas import PromptOptimizeRequest
from app.services import brand_style, merchant_profile, prompt_optimizer
from app.services.scoped_db import ScopedDatabase


def _context_db():
    db = ScopedDatabase(mongomock.MongoClient().db, "merchant")
    db.brands.insert_one({
        "_id": "style", "name": "Style", "layout_guide": "Layout", "background_guide": "Background",
        "merchant_id": "merchant", "platform_id": "instagram", "placement_id": "story",
    })
    db.assets.insert_one({"_id": "product", "prepared_url": "https://example.test/product.png"})
    db.merchant_profiles.insert_one({"_id": "merchant", "name": "LOCAL_LEGACY", "sections": [
        {"source_path": "brand.positioning", "text": "LOCAL_MUST_NOT_REACH_MODEL"},
    ]})
    return db


def _am_profile():
    return merchant_profile.project_profile({
        "merchant_id": "merchant", "brand": {"canonical_name": "AM_MERCHANT", "positioning": "AM_POSITIONING"},
        "locations": [{"place_id": "p1", "core_highlights": ["AM_HIGHLIGHT"], "nap": {"address": "PRIVATE_ADDRESS"}}],
    })


def test_brand_snapshot_uses_authenticated_am_background(settings, store):
    db = _context_db()
    brand = brand_style.get_brand(db, "style")
    snapshot = brand_style.snapshot_brand(brand, db, store, settings, profile=_am_profile())
    assert "AM_POSITIONING" in snapshot["merchant_context"]
    assert "AM_HIGHLIGHT" in snapshot["merchant_context"]
    assert "LOCAL_" not in snapshot["merchant_context"]
    assert "PRIVATE_ADDRESS" not in snapshot["merchant_context"]
    assert brand_style.snapshot_brand(brand, db, store, settings)["merchant_context"] == ""


@pytest.mark.parametrize("brand_mode", ["preset", "none", "reference"])
def test_optimizer_uses_authenticated_am_background(settings, store, brand_mode):
    db = _context_db()

    class Client:
        def run_agent(self, agent_id, input_text, *, timeout_seconds):
            self.input_text = input_text
            return json.dumps({"prompt": "Optimized poster"})

    client = Client()
    prompt_optimizer.optimize_prompt(
        db, settings, store, client,
        request=PromptOptimizeRequest(brand_mode=brand_mode, brand_id="style" if brand_mode == "preset" else None,
                                      product_asset_ids=["product"]),
        profile=_am_profile(),
    )
    assert "AM_POSITIONING" in client.input_text
    assert "LOCAL_" not in client.input_text
    assert "PRIVATE_ADDRESS" not in client.input_text


@pytest.mark.parametrize("profile", [_am_profile(), None])
@pytest.mark.parametrize("include_address", [True, False])
def test_optimizer_route_passes_optional_authenticated_profile(settings, store, profile, include_address):
    from app.routes.prompts import optimize_prompt

    class Client:
        def run_agent(self, agent_id, input_text, *, timeout_seconds):
            self.input_text = input_text
            return json.dumps({"prompt": "Optimized poster"})

    client = Client()
    request = SimpleNamespace(
        state=SimpleNamespace(db=_context_db(), store=store, merchant_profile=profile),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, core_ai=client)),
    )
    assert optimize_prompt(PromptOptimizeRequest(brand_mode="none", product_asset_ids=["product"],
                                                store_place_id="p1", include_store_address=include_address), request)
    assert ("AM_POSITIONING" in client.input_text) == bool(profile)
    assert ("PRIVATE_ADDRESS" in client.input_text) == bool(profile and include_address)
    assert "LOCAL_" not in client.input_text


@pytest.mark.parametrize("brand_mode", ["preset", "none"])
@pytest.mark.parametrize("profile", [_am_profile(), None])
def test_generation_snapshots_only_selected_store_fields(settings, store, brand_mode, profile):
    from fastapi import BackgroundTasks
    from app.routes.whole_image import create_task
    from app.schemas import WholeImageTaskCreate

    db = _context_db()
    request = SimpleNamespace(
        state=SimpleNamespace(db=db, store=store, merchant_profile=profile),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, core_ai=None)),
    )
    result = create_task(WholeImageTaskCreate(
        prompt="Poster", brand_mode=brand_mode, brand_id="style" if brand_mode == "preset" else None,
        store_place_id="p1", include_store_address=True, include_store_phone=False,
    ), request, BackgroundTasks())
    context = db.whole_tasks.find_one({"_id": result["id"]})["merchant_context"]
    assert ("PRIVATE_ADDRESS" in context) == bool(profile)
    assert "LOCAL_" not in context
