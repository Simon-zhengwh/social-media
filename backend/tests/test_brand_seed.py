from __future__ import annotations

import io

from types import SimpleNamespace

import mongomock
import pytest
from fastapi import HTTPException
from PIL import Image, ImageDraw

from app.routes import brands as brand_routes
from app.services import brand_seed, brand_style
from app.services.scoped_db import ScopedDatabase

MERCHANT = "f0750173-0113-464d-bb90-cbebb9c08ba7"


def _logo_bytes(colors=((200,30,30), (20,120,60)), size=(120,120)) -> bytes:
    image = Image.new("RGB", size, (255,255,255))
    draw = ImageDraw.Draw(image)
    half = size[0] //2
    draw.rectangle((0,0, half, size[1]), fill=colors[0])
    draw.rectangle((half,0, size[0], size[1]), fill=colors[1 % len(colors)])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _profile(**overrides) -> dict:
    profile = {
        "merchant_id": MERCHANT,
        "merchant_name": "Keke Food",
        "brand": {
            "canonical_name": "Keke Food可可小卤",
            "name_variants": ["Keke", "可可小卤"],
            "primary_category": "Restaurant",
            "cuisine": ["Chinese", "Braised"],
            "positioning": "Fast casual braised snacks for office workers. Second sentence.",
            "personality": "Playful",
            "voice": {"description": "Warm and direct"},
            "logo_url": "https://cdn.example.com/keke.png",
        },
        "locations": [{"place_id": "p1", "name": "Downtown"}],
    }
    profile["brand"].update(overrides)
    return profile


def _seed(db, settings, store, profile, *, fetch_image=None):
    return brand_seed.initialize(
        db, profile, settings=settings, store=store, fetch_image=fetch_image
    )


def test_derives_document_from_am_profile(settings, store):
    db = ScopedDatabase(mongomock.MongoClient().db, MERCHANT)

    document, created = _seed(db, settings, store, _profile(), fetch_image=lambda url: _logo_bytes())

    assert created is True
    assert document["_id"] == MERCHANT
    assert document["merchant_id"] == MERCHANT
    assert document["name"] == "Keke Food可可小卤"
    assert document["platform_id"] == "xiaohongshu"
    assert document["placement_id"] == "note"
    assert document["is_default"] is True
    assert document["seeded_from"] == brand_seed.SEED_SOURCE
    assert sorted(document["logo_keywords"]) == sorted(["keke food可可小卤", "keke", "可可小卤"])
    assert document["summary"].startswith("Restaurant")
    assert "Second sentence" not in document["summary"]
    assert document["style_fields"]["subject_style"]
    assert document["style_fields"]["typography"]
    assert document["style_fields"]["background"]
    assert len(document["logo_asset_ids"]) == 1
    assert len(document["colors"]) >= 1
    assert len(document["colors"]) <= 6
    for color in document["colors"]:
        assert brand_seed.HEX_PATTERN.fullmatch(color["hex"])
    # The stored document must be readable through the production brand reader.
    assert brand_style.brand_from_document(document).id == MERCHANT


def test_seed_is_idempotent(settings, store):
    db = ScopedDatabase(mongomock.MongoClient().db, MERCHANT)

    first, created_first = _seed(db, settings, store, _profile(), fetch_image=lambda url: _logo_bytes())
    second, created_second = _seed(db, settings, store, _profile(), fetch_image=lambda url: _logo_bytes())

    assert created_first is True
    assert created_second is False
    assert second["_id"] == first["_id"]
    assert db.brands.count_documents({}) == 1
    assert db.assets.count_documents({}) == 1


def test_missing_logo_still_creates_brand(settings, store):
    db = ScopedDatabase(mongomock.MongoClient().db, MERCHANT)

    document, created = _seed(
        db, settings, store, _profile(logo_url=""), fetch_image=lambda url: _logo_bytes()
    )

    assert created is True
    assert document["logo_asset_ids"] == []
    assert document["colors"] == []
    assert brand_style.brand_from_document(document).name == "Keke Food可可小卤"


def test_logo_download_failure_is_skipped(settings, store):
    db = ScopedDatabase(mongomock.MongoClient().db, MERCHANT)

    def boom(_url: str) -> bytes:
        raise RuntimeError("network down")

    document, created = _seed(db, settings, store, _profile(), fetch_image=boom)

    assert created is True
    assert document["logo_asset_ids"] == []
    assert document["colors"] == []


def test_profile_without_brand_name_is_rejected(settings, store):
    db = ScopedDatabase(mongomock.MongoClient().db, MERCHANT)

    with pytest.raises(brand_seed.SeedError) as exc:
        _seed(db, settings, store, _profile(canonical_name=""), fetch_image=lambda url: _logo_bytes())

    assert exc.value.code == "merchant_profile_incomplete"
    assert db.brands.count_documents({}) == 0


def test_seed_stays_inside_the_requesting_merchant(settings, store):
    raw = mongomock.MongoClient().db

    _seed(
        ScopedDatabase(raw, MERCHANT), settings, store, _profile(),
        fetch_image=lambda url: _logo_bytes(),
    )
    document, created = _seed(
        ScopedDatabase(raw, "another-merchant"), settings, store,
        {**_profile(), "merchant_id": "another-merchant"},
    )

    assert created is True
    assert document["merchant_id"] == "another-merchant"
    assert raw.brands.count_documents({"merchant_id": MERCHANT}) == 1
    assert raw.brands.count_documents({"merchant_id": "another-merchant"}) == 1


def test_extract_palette_drops_near_white_background():
    colors = brand_seed.extract_palette(_logo_bytes())

    hexes = [item["hex"] for item in colors]
    assert "#FFFFFF" not in hexes
    assert len(hexes) == len(set(hexes))


def _route_request(db, settings, store, profile=None, *, fetch=None, monkeypatch=None):
    """A request shaped like the merchant middleware leaves it."""
    if fetch is None:
        fetch = lambda url: _logo_bytes()
    if monkeypatch is not None:
        monkeypatch.setattr(brand_routes, "_fetch_remote_image", fetch)
    return SimpleNamespace(
        state=SimpleNamespace(
            db=ScopedDatabase(db, MERCHANT),
            merchant_id=MERCHANT,
            merchant_profile=profile,
        ),
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, store=store)),
    )


def test_route_seeds_style_from_projected_profile(settings, store, monkeypatch):
    from app.services.merchant_profile import project_profile

    db = mongomock.MongoClient().db
    profile = project_profile(_profile())
    request = _route_request(db, settings, store, profile, monkeypatch=monkeypatch)

    response = brand_routes.initialize_from_profile(request)

    assert response["created"] is True
    assert response["id"] == MERCHANT
    assert response["name"] == "Keke Food可可小卤"
    assert response["merchant_profile"] is not None
    assert db.brands.count_documents({"merchant_id": MERCHANT}) == 1


def test_route_is_idempotent(settings, store, monkeypatch):
    from app.services.merchant_profile import project_profile

    db = mongomock.MongoClient().db
    profile = project_profile(_profile())

    first = brand_routes.initialize_from_profile(
        _route_request(db, settings, store, profile, monkeypatch=monkeypatch)
    )
    second = brand_routes.initialize_from_profile(
        _route_request(db, settings, store, profile, monkeypatch=monkeypatch)
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["id"] == first["id"]
    assert db.brands.count_documents({"merchant_id": MERCHANT}) == 1


def test_route_rejects_merchant_without_profile(settings, store, monkeypatch):
    db = mongomock.MongoClient().db
    request = _route_request(db, settings, store, None, monkeypatch=monkeypatch)

    with pytest.raises(HTTPException) as exc:
        brand_routes.initialize_from_profile(request)

    assert exc.value.status_code == 422
    assert db.brands.count_documents({}) == 0
