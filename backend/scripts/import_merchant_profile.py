"""Import a source-backed merchant record and explicitly link existing styles."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database


def validate_profile(profile: dict) -> None:
    if profile["source_record_id"] != profile["source"]["_id"]:
        raise ValueError("Source record ID mismatch")
    for section in profile["sections"]:
        value = profile["source"]
        for part in section["source_path"].split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        if section["original"] != value:
            raise ValueError(f"Source mismatch: {section['source_path']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", type=Path)
    parser.add_argument("--brand-id", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    validate_profile(profile)
    settings = get_settings()
    with create_client(settings.mongodb_uri) as client:
        db = get_database(client, settings.mongodb_database)
        for brand_id in args.brand_id:
            brand = db.brands.find_one({"_id": brand_id})
            if brand is None or brand.get("merchant_id") not in (None, profile["_id"]):
                raise ValueError(f"Missing style or conflicting merchant link: {brand_id}")
        existing = db.merchant_profiles.find_one({"_id": profile["_id"]})
        if existing is not None and existing != profile:
            raise ValueError("Existing merchant profile differs; refusing to overwrite")
        if args.apply:
            db.merchant_profiles.update_one({"_id": profile["_id"]}, {"$setOnInsert": profile}, upsert=True)
            for brand_id in args.brand_id:
                db.brands.update_one({"_id": brand_id}, {"$set": {"merchant_id": profile["_id"]}})
        print(json.dumps({"applied": args.apply, "merchant_id": profile["_id"], "brand_ids": args.brand_id}, ensure_ascii=False))


if __name__ == "__main__":
    main()
