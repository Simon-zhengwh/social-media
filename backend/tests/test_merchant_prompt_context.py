import json

import mongomock

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
        "locations": [{"core_highlights": ["AM_HIGHLIGHT"], "nap": {"address": "PRIVATE_ADDRESS"}}],
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


def test_optimizer_uses_authenticated_am_background(settings, store):
    db = _context_db()

    class Client:
        def run_agent(self, agent_id, input_text, *, timeout_seconds):
            self.input_text = input_text
            return json.dumps({"prompt": "Optimized poster"})

    client = Client()
    prompt_optimizer.optimize_prompt(
        db, settings, store, client,
        request=PromptOptimizeRequest(brand_mode="preset", brand_id="style", product_asset_ids=["product"]),
        profile=_am_profile(),
    )
    assert "AM_POSITIONING" in client.input_text
    assert "LOCAL_" not in client.input_text
    assert "PRIVATE_ADDRESS" not in client.input_text
