from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services import brand_style
from app.services.object_store import create_object_store


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import brand skills from .agents/skills into MongoDB."
    )
    parser.add_argument(
        "--skills-dir",
        default="../.agents/skills",
        help="Skill root directory. Relative paths resolve from backend/.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing brand documents.",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Only import the brand with this id.",
    )
    args = parser.parse_args()

    skills_dir = Path(args.skills_dir)
    if not skills_dir.is_absolute():
        skills_dir = (BACKEND_DIR / skills_dir).resolve()

    settings = get_settings()
    store = create_object_store(settings)
    mongo = create_client(settings.mongodb_uri)
    db = get_database(mongo, settings.mongodb_database)
    try:
        result = brand_style.import_brand_skills(
            db,
            store,
            settings,
            skills_dir,
            force=args.force,
            only=args.only,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["errors"]:
            raise SystemExit(1)
    finally:
        mongo.close()


if __name__ == "__main__":
    main()
