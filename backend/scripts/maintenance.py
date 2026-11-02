from __future__ import annotations

import argparse
import json

from app.config import get_settings
from app.db import create_client, get_database
from app.services import maintenance


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the social-media shared maintenance gate")
    parser.add_argument("command", choices=["enable", "status", "wait", "disable"])
    parser.add_argument("--timeout-seconds", type=int, default=0)
    args = parser.parse_args()

    settings = get_settings()
    client = create_client(settings.mongodb_uri)
    try:
        db = get_database(client, settings.mongodb_database)
        maintenance.initialize(db)
        if args.command == "enable":
            result = maintenance.enable(db)
        elif args.command == "disable":
            result = maintenance.disable(db)
        elif args.command == "wait":
            result = maintenance.wait_until_drained(db, timeout_seconds=args.timeout_seconds)
        else:
            result = maintenance.status(db)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if not result.get("timed_out") else 2
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
