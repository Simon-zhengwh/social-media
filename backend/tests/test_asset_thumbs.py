from __future__ import annotations

import io
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException
from PIL import Image

from app.api_paths import asset_thumb_url
from app.routes import assets as asset_routes
from app.routes import whole_image as whole_routes
from app.services import asset_thumbs


def _poster_png(width: int = 1080, height: int = 1350) -> bytes:
    image = Image.new("RGB", (width, height), (200, 120, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _request(db, store, settings):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, store=store, settings=settings))
    )


def _seed_asset(db, store, settings, *, width=1080, height=1350, mime="image/png"):
    data = _poster_png(width, height)
    store.put_bytes(f"prepared/asset_poster.png", data, mime)
    db.assets.insert_one(
        {
            "_id": "asset_poster",
            "merchant_id": "m1",
            "path": "prepared/asset_poster.png",
            "prepared_path": "prepared/asset_poster.png",
            "mime_type": mime,
        }
    )
    return data


def test_asset_thumb_url_points_at_the_light_endpoint():
    assert asset_thumb_url("m1", "asset_1") == "/api/merchants/m1/assets/asset_1/thumb?w=480"
    assert asset_thumb_url("m1", "asset_1", width=240).endswith("/thumb?w=240")
    assert asset_thumb_url("", "asset_1") == "/api/assets/asset_1/thumb?w=480"


def test_render_thumb_downscales_and_keeps_the_canvas_ratio():
    data = asset_thumbs.render_thumb(_poster_png(1080, 1350), 480)

    with Image.open(io.BytesIO(data)) as thumb:
        assert thumb.format == "WEBP"
        assert thumb.size == (480, 600)
    assert len(data) < 60_000


def test_render_thumb_keeps_transparency_and_never_upscales():
    image = Image.new("RGBA", (200, 120), (0, 0, 0, 0))
    for x in range(20, 180):
        for y in range(20, 100):
            image.putpixel((x, y), (10, 200, 10, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    with Image.open(io.BytesIO(asset_thumbs.render_thumb(buffer.getvalue(), 480))) as thumb:
        assert thumb.size == (200, 120)
        assert thumb.mode == "RGBA"


def test_clamp_width_bounds_the_requested_size():
    assert asset_thumbs.clamp_width(None) == asset_thumbs.DEFAULT_WIDTH
    assert asset_thumbs.clamp_width(10) == asset_thumbs.MIN_WIDTH
    assert asset_thumbs.clamp_width(99999) == asset_thumbs.MAX_WIDTH


def test_thumb_route_serves_webp_and_caches_it(settings, store):
    db = mongomock.MongoClient().db
    original = _seed_asset(db, store, settings)
    request = _request(db, store, settings)

    first = asset_routes.get_asset_thumb("asset_poster", request, 480)

    assert first.status_code == 200
    assert first.media_type == "image/webp"
    assert first.headers["cache-control"] == asset_routes.THUMB_CACHE_CONTROL
    assert len(first.body) < len(original)
    cached = asset_thumbs.cache_path(settings, "asset_poster", 480)
    assert cached.is_file() and cached.read_bytes() == first.body


def test_thumb_route_reads_the_cache_without_touching_the_source(settings, store):
    db = mongomock.MongoClient().db
    _seed_asset(db, store, settings)
    request = _request(db, store, settings)
    asset_routes.get_asset_thumb("asset_poster", request, 480)
    # The source object is gone, so a second hit can only be served from the thumb cache.
    store.delete("prepared/asset_poster.png")

    response = asset_routes.get_asset_thumb("asset_poster", request, 480)

    assert response.status_code == 200
    assert len(response.body) > 0


def test_thumb_route_404s_for_an_unknown_asset(settings, store):
    request = _request(mongomock.MongoClient().db, store, settings)

    with pytest.raises(HTTPException) as exc:
        asset_routes.get_asset_thumb("asset_missing", request, 480)
    assert exc.value.status_code == 404


def test_candidate_summary_carries_a_thumb_url():
    doc = {
        "_id": "wtask_1",
        "merchant_id": "m1",
        "status": "succeeded",
        "output_asset_id": "asset_poster",
        "current_version": 2,
        "selected_version_id": "wver_1",
    }

    summary = whole_routes._candidate_summary(doc)

    assert summary["preview_url"] == "/api/merchants/m1/assets/asset_poster/file?v=2"
    assert summary["thumb_url"] == "/api/merchants/m1/assets/asset_poster/thumb?w=480"


def test_candidate_summary_has_no_thumb_without_an_output():
    summary = whole_routes._candidate_summary({"_id": "wtask_2", "merchant_id": "m1", "status": "queued"})

    assert summary["preview_url"] is None
    assert summary["thumb_url"] is None


def test_am_item_rows_expose_a_thumb_url_for_our_own_assets():
    from app.services import am_assets

    media = {"id": "am1", "name": "dish.png", "thumbnail_image_url": "https://am/x.png"}
    plain = am_assets._am_item(media)
    assert plain["thumb_url"] is None
    assert plain["url"] == "https://am/x.png"

    ours = am_assets._am_item(media, {"_id": "asset_1", "merchant_id": "m1", "original_name": "dish.png"})
    assert ours["thumb_url"] == "/api/merchants/m1/assets/asset_1/thumb?w=480"
    assert ours["url"] == "https://am/x.png"


def test_local_item_rows_expose_a_thumb_url():
    from app.services import am_assets

    row = am_assets._local_item("m1", {"_id": "asset_2", "original_name": "dish.png"})
    assert row["thumb_url"] == "/api/merchants/m1/assets/asset_2/thumb?w=480"
    assert row["url"] == "/api/merchants/m1/assets/asset_2/file"
