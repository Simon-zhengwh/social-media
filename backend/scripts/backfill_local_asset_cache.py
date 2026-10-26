from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = BACKEND_DIR.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings
from app.db import create_client, get_database
from app.services import asset_records, storage
from app.services.object_store import LocalObjectStore



def _image_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in storage.SUPPORTED_IMAGE_SUFFIXES:
            yield path


def _source_hashes(roots: Iterable[Path]) -> dict[str, bytes]:
    indexed: dict[str, bytes] = {}
    for root in roots:
        for path in _image_files(root):
            data = path.read_bytes()
            indexed.setdefault(hashlib.sha256(data).hexdigest(), data)
    return indexed


def _git_hashes(ref: str) -> dict[str, bytes]:
    listing = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", "-z", ref],
        cwd=PROJECT_DIR,
        check=True,
        capture_output=True,
    ).stdout
    indexed: dict[str, bytes] = {}
    for raw_path in listing.split(b"\0"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8")
        if Path(path).suffix.lower() not in storage.SUPPORTED_IMAGE_SUFFIXES:
            continue
        data = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            cwd=PROJECT_DIR,
            check=True,
            capture_output=True,
        ).stdout
        indexed.setdefault(hashlib.sha256(data).hexdigest(), data)
    return indexed


def backfill(
    db: Any,
    settings: Any,
    *,
    source_roots: list[Path] | None = None,
    git_ref: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    project_dir = Path(__file__).resolve().parents[2]
    roots = source_roots or [
        project_dir / "朵颐素材",
        project_dir / "inspost素材",
        settings.skills_path,
    ]
    indexed = _source_hashes(roots)
    if git_ref:
        for digest, data in _git_hashes(git_ref).items():
            indexed.setdefault(digest, data)

    cache = LocalObjectStore(settings.storage_path / "objects")
    candidates = list(db.assets.find(asset_records.uploaded_asset_query()))
    result: dict[str, Any] = {
        "mode": "apply" if apply else "preview",
        "matched": [],
        "already_cached": [],
        "missing_source": [],
    }
    for asset in candidates:
        asset_id = str(asset["_id"])
        original_key = str(asset.get("path") or "")
        prepared_key = str(asset.get("prepared_path") or original_key)
        if original_key and prepared_key and cache.exists(original_key) and cache.exists(prepared_key):
            result["already_cached"].append(asset_id)
            continue
        data = indexed.get(str(asset.get("sha256") or ""))
        if data is None:
            result["missing_source"].append(asset_id)
            continue
        result["matched"].append(asset_id)
        if not apply:
            continue
        with tempfile.TemporaryDirectory() as directory:
            temporary_store = LocalObjectStore(Path(directory))
            prepared = storage.prepare_upload(
                settings,
                temporary_store,
                data,
                str(asset.get("original_name") or "asset.png"),
            )
            prepared_data = temporary_store.get_bytes(prepared.prepared_path)
        cache.put_bytes(original_key, data, str(asset.get("mime_type") or "application/octet-stream"))
        cache.put_bytes(prepared_key, prepared_data, "image/png")

    if apply:
        result["restored"] = result.pop("matched")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write matched files into the local object cache.")
    parser.add_argument("--git-ref", default=None, help="Optionally match source blobs from a historical Git ref.")
    args = parser.parse_args()

    settings = get_settings()
    client = create_client(settings.mongodb_uri)
    try:
        result = backfill(
            get_database(client, settings.mongodb_database),
            settings,
            git_ref=args.git_ref,
            apply=args.apply,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
