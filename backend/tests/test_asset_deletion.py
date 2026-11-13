from __future__ import annotations

from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException

from app.routes.assets import AssetDeletePayload, delete_asset, delete_asset_batch
from app.services import asset_deletion, storage
from app.services.am_assets import AM_ASSET_PREFIX


class FakeAM:
    """Records the media-library delete calls AM would receive."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def delete_media(self, cookie: str, merchant_id: str, media_ids: list[str]) -> None:
        assert cookie, "调用 AM 删除必须带会话 Cookie"
        self.batches.append(list(media_ids))


@pytest.fixture
def am() -> FakeAM:
    return FakeAM()


def _request(db, store, am=None, cookie: str = "AMSessionId=test"):
    state = SimpleNamespace(db=db, store=store, am_cookie=cookie)
    app_state = SimpleNamespace(db=db, store=store, am=am)
    return SimpleNamespace(app=SimpleNamespace(state=app_state), state=state)


def _upload(settings, store, product_png, db, name: str = "dish.png", media_id: str = ""):
    prepared = storage.prepare_upload(settings, store, product_png, name)
    document = {
        "_id": prepared.id,
        "source": "upload",
        "original_name": prepared.original_name,
        "path": prepared.path,
        "prepared_path": prepared.prepared_path,
        "mime_type": prepared.mime_type,
    }
    if media_id:
        document["am_media_library_id"] = media_id
    db.assets.insert_one(document)
    return prepared


def test_deleting_an_upload_also_removes_its_am_record(settings, store, product_png, am):
    db = mongomock.MongoClient().db
    prepared = _upload(settings, store, product_png, db, media_id="media-1")
    request = _request(db, store, am)

    response = delete_asset_batch(request, AssetDeletePayload(ids=[prepared.id]))

    assert response["am_deleted"] == ["media-1"]
    assert am.batches == [["media-1"]]
    assert db.assets.find_one({"_id": prepared.id}) is None
    assert not store.exists(prepared.path)
    assert not store.exists(prepared.prepared_path)


def test_an_am_only_asset_is_deleted_through_its_media_id(store, am):
    db = mongomock.MongoClient().db
    request = _request(db, store, am)

    response = delete_asset_batch(
        request, AssetDeletePayload(ids=[f"{AM_ASSET_PREFIX}media-9"], force=False)
    )

    assert am.batches == [["media-9"]]
    assert response["local_deleted"] == []
    assert response["deleted"] == [f"{AM_ASSET_PREFIX}media-9"]


def test_a_batch_is_sent_to_am_in_chunks(store, am):
    db = mongomock.MongoClient().db
    request = _request(db, store, am)
    ids = [f"{AM_ASSET_PREFIX}media-{index}" for index in range(25)]

    delete_asset_batch(request, AssetDeletePayload(ids=ids))

    assert [len(batch) for batch in am.batches] == [
        asset_deletion.AM_BATCH_SIZE,
        25 - asset_deletion.AM_BATCH_SIZE,
    ]
    assert sum(len(batch) for batch in am.batches) == 25


def test_a_referenced_asset_needs_force_before_am_is_touched(settings, store, product_png, am):
    db = mongomock.MongoClient().db
    prepared = _upload(settings, store, product_png, db, media_id="media-2")
    db.brands.insert_one({"_id": "brand_1", "reference_asset_ids": [prepared.id]})
    request = _request(db, store, am)

    with pytest.raises(HTTPException) as exc:
        delete_asset_batch(request, AssetDeletePayload(ids=[prepared.id]))
    assert exc.value.status_code == 409
    assert am.batches == []
    assert db.assets.find_one({"_id": prepared.id}) is not None

    delete_asset_batch(request, AssetDeletePayload(ids=[prepared.id], force=True))

    assert am.batches == [["media-2"]]
    assert db.assets.find_one({"_id": prepared.id}) is None


def test_an_unknown_asset_reports_not_found(store, am):
    db = mongomock.MongoClient().db
    request = _request(db, store, am)

    with pytest.raises(HTTPException) as exc:
        delete_asset("asset_missing", request)
    assert exc.value.status_code == 404
    assert am.batches == []


def test_a_generated_result_cannot_be_deleted(store, am):
    db = mongomock.MongoClient().db
    db.assets.insert_one({"_id": "asset_generated", "source": "generated", "path": "p.png"})
    request = _request(db, store, am)

    with pytest.raises(HTTPException) as exc:
        delete_asset("asset_generated", request)
    assert exc.value.status_code == 422


def test_single_delete_still_returns_204(settings, store, product_png, am):
    db = mongomock.MongoClient().db
    prepared = _upload(settings, store, product_png, db)
    request = _request(db, store, am)

    response = delete_asset(prepared.id, request)

    assert response.status_code == 204
    assert am.batches == [], "没有 AM 映射的本地素材不应调用 AM"


def test_deleting_without_an_am_session_refuses_an_am_backed_asset(settings, store, product_png):
    db = mongomock.MongoClient().db
    prepared = _upload(settings, store, product_png, db, media_id="media-3")
    request = _request(db, store, None)

    with pytest.raises(HTTPException) as exc:
        delete_asset(prepared.id, request)
    assert exc.value.status_code == 502
    assert db.assets.find_one({"_id": prepared.id}) is not None
