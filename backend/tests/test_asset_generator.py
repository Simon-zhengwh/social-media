from __future__ import annotations

from datetime import datetime, timezone

import mongomock

from app.services import asset_generator


def test_initial_logo_is_not_duplicated_when_logo_is_managed_explicitly():
    db = mongomock.MongoClient().db
    now = datetime.now(timezone.utc)
    db.assets.insert_one(
        {
            "_id": "asset_logo",
            "kind": "logo",
            "original_name": "logo.png",
            "path": "logos/logo.png",
            "prepared_path": "prepared/logo.png",
            "sha256": "abc",
            "created_at": now,
        }
    )
    plan = {
        "design_intent": {},
        "asset_requests": [
            {"id": "asset_req_logo_01", "role": "logo", "asset_id": "asset_logo"}
        ],
        "initial_scene_plan": {
            "layers": [
                {
                    "id": "layer_logo_01",
                    "type": "image",
                    "asset_id": "asset_logo",
                    "box": {"x": 900, "y": 50, "width": 100, "height": 100},
                }
            ]
        },
    }

    image_layers, _, usage = asset_generator.build_scene_assets(
        plan=plan,
        canvas={"width": 1080, "height": 1440},
        products=[],
        logo=db.assets.find_one({"_id": "asset_logo"}),
        db=db,
    )

    assert [layer["id"] for layer in image_layers].count("layer_logo_01") == 1
    assert [layer["role"] for layer in image_layers] == ["logo"]
    assert [item["role"] for item in usage] == ["logo"]
