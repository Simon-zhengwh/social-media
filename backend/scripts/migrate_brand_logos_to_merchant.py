"""Move per-style brand logos onto the merchant-level `merchant_logos` collection.

Run once before the release that stops reading `brands.logo_asset_ids`; the old field is kept
so a rollback still shows logos. Idempotent: a merchant that already has a record is skipped.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services.merchant_logos import MAX_MERCHANT_LOGOS


def _merge(styles: list[dict]) -> list[str]:
    ids: list[str] = []
    for style in styles:
        for asset_id in style.get("logo_asset_ids") or []:
            value = str(asset_id)
            if value and value not in ids:
                ids.append(value)
    return ids[:MAX_MERCHANT_LOGOS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merchant-id", action="append", help="limit to specific merchants")
    parser.add_argument("--apply", action="store_true", help="write records; default is a dry run")
    args = parser.parse_args()
    settings = get_settings()
    with create_client(settings.mongodb_uri) as client:
        db = get_database(client, settings.mongodb_database)
        query: dict = {"merchant_id": {"$in": args.merchant_id}} if args.merchant_id else {}
        merchant_ids = sorted(
            str(value) for value in db.brands.distinct("merchant_id", query) if value
        )
        created = 0
        for merchant_id in merchant_ids:
            if db.merchant_logos.find_one({"_id": merchant_id}) is not None:
                print(f"{merchant_id}: already migrated, skipped")
                continue
            styles = list(
                db.brands.find({"merchant_id": merchant_id}).sort(
                    [("is_default", -1), ("created_at", 1), ("_id", 1)]
                )
            )
            logo_ids = _merge(styles)
            print(f"{merchant_id}: {len(styles)} style(s) -> logo_asset_ids={logo_ids}")
            if not logo_ids:
                continue
            created += 1
            if args.apply:
                db.merchant_logos.insert_one(
                    {
                        "_id": merchant_id,
                        "merchant_id": merchant_id,
                        "logo_asset_ids": logo_ids,
                        "updated_at": datetime.now(timezone.utc),
                    }
                )
        print(f"{'migrated' if args.apply else 'would migrate'} {created} merchant(s)")


if __name__ == "__main__":
    main()
