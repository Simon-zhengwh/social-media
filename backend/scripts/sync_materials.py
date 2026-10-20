from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services.object_store import create_object_store
from app.services.sample_assets import import_samples

MANAGED_KINDS = {"product", "logo", "style", "decoration"}


def main() -> None:
    settings = get_settings()
    store = create_object_store(settings)
    mongo = create_client(settings.mongodb_uri)
    db = get_database(mongo, settings.mongodb_database)
    try:
        existing = list(db.assets.find({"kind": {"$in": list(MANAGED_KINDS)}}))
        for asset in existing:
            for key in {asset.get("path"), asset.get("prepared_path")}:
                if key:
                    store.delete(key)
            db.assets.delete_one({"_id": asset["_id"]})
        result = import_samples(db, settings, store)
        print(
            json.dumps(
                {
                    "removed": len(existing),
                    "imported": len(result["imported"]),
                    "skipped": result["skipped"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        mongo.close()


if __name__ == "__main__":
    main()
