from types import SimpleNamespace

import pytest

from app import config, main
from app.config import Settings
from app.services import object_store


@pytest.fixture
def uncached_settings(monkeypatch):
    monkeypatch.delenv("SOCIAL_MEDIA_ENV_FILE", raising=False)
    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def test_explicit_env_file_loads_local_settings_without_default_credentials(tmp_path, monkeypatch, uncached_settings):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "AM_BYPASS_LOCAL=1\nAM_BYPASS_MERCHANT_ID=demo\n"
        "SOCIAL_MEDIA_ORIGIN=http://127.0.0.1:5173\nMONGODB_DATABASE=existing_demo\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOCIAL_MEDIA_ENV_FILE", str(env_file))
    settings = config.get_settings()
    assert settings.am_bypass_local
    assert settings.am_bypass_merchant_id == "demo"
    assert settings.mongodb_database == "existing_demo"
    assert settings.core_ai_api_key == ""
    assert settings.am_media_storage_service_key == ""


def test_missing_explicit_env_file_fails_instead_of_loading_deployment_settings(tmp_path, monkeypatch, uncached_settings):
    monkeypatch.setenv("SOCIAL_MEDIA_ENV_FILE", str(tmp_path / "missing.env"))
    with pytest.raises(ValueError, match="SOCIAL_MEDIA_ENV_FILE"):
        config.get_settings()


def test_relative_env_file_is_resolved_from_backend_not_working_directory(tmp_path, monkeypatch, uncached_settings):
    (tmp_path / ".env.local").write_text("MONGODB_DATABASE=relative_demo\n", encoding="utf-8")
    monkeypatch.setattr(config, "BACKEND_DIR", tmp_path)
    monkeypatch.setenv("SOCIAL_MEDIA_ENV_FILE", ".env.local")
    assert config.get_settings().mongodb_database == "relative_demo"


def test_process_environment_still_overrides_selected_env_file(tmp_path, monkeypatch, uncached_settings):
    env_file = tmp_path / "local.env"
    env_file.write_text("MONGODB_DATABASE=from_file\n", encoding="utf-8")
    monkeypatch.setenv("SOCIAL_MEDIA_ENV_FILE", str(env_file))
    monkeypatch.setenv("MONGODB_DATABASE", "from_process")
    assert config.get_settings().mongodb_database == "from_process"


def test_normal_startup_does_not_automatically_read_local_env(tmp_path, monkeypatch, uncached_settings):
    default_file = tmp_path / ".env"
    default_file.write_text("MONGODB_DATABASE=deployed\nAM_BYPASS_LOCAL=0\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("MONGODB_DATABASE=local\nAM_BYPASS_LOCAL=1\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", str(default_file))
    settings = config.get_settings()
    assert settings.mongodb_database == "deployed"
    assert settings.am_bypass_local is False


def test_local_mode_forces_disk_even_when_cos_credentials_are_present(tmp_path):
    settings = Settings(
        _env_file=None, storage_dir=str(tmp_path), am_bypass_local=True,
        social_media_origin="http://127.0.0.1:5173", cos_secret_id="test-id",
        cos_secret_key="test-key", cos_bucket="bucket", cos_region="region",
    )
    store = object_store.create_object_store(settings)
    assert isinstance(store, object_store.LocalObjectStore)
    store.put_bytes("example.txt", b"local")
    assert (tmp_path / "objects/example.txt").read_bytes() == b"local"


@pytest.mark.parametrize("bypass,origin", [(False, "http://127.0.0.1:5173"), (True, "https://social.example")])
def test_deployed_store_selection_is_preserved(tmp_path, monkeypatch, bypass, origin):
    expected = object()
    monkeypatch.setattr(object_store, "CosObjectStore", lambda settings: expected)
    settings = Settings(
        _env_file=None, storage_dir=str(tmp_path), am_bypass_local=bypass,
        social_media_origin=origin, cos_secret_id="test-id", cos_secret_key="test-key",
        cos_bucket="bucket", cos_region="region",
    )
    assert object_store.create_object_store(settings) is expected


def test_background_recovery_uses_local_store_despite_am_service_credentials(monkeypatch):
    local_store = object()
    settings = Settings(
        _env_file=None, am_bypass_local=True, social_media_origin="http://127.0.0.1:5173",
        am_media_storage_service_key="present-but-unused-locally",
    )
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, local_store=local_store, am=object()))
    monkeypatch.setattr(main, "create_merchant_store", lambda *args: pytest.fail("Local recovery called AM storage"))
    assert main._scoped_store(app, "demo", object()) is local_store


@pytest.fixture
def demo_data(tmp_path):
    import mongomock

    settings = Settings(
        _env_file=None, storage_dir=str(tmp_path), am_bypass_local=True,
        am_bypass_merchant_id="demo", social_media_origin="http://127.0.0.1:5173",
        core_ai_api_key="test-key", mongodb_uri="mongodb://127.0.0.1:27018",
    )
    (tmp_path / "objects/prepared").mkdir(parents=True)
    (tmp_path / "objects/prepared/present.png").write_bytes(b"test")
    db = mongomock.MongoClient().db
    db.assets.insert_many([
        {"_id": "present", "merchant_id": "demo", "path": "prepared/present.png"},
        {"_id": "missing", "merchant_id": "demo", "path": "prepared/missing.png"},
        {"_id": "other", "merchant_id": "other", "path": "prepared/missing.png"},
    ])
    db.brands.insert_one({"_id": "brand", "merchant_id": "demo"})
    db.whole_tasks.insert_one({"_id": "task", "merchant_id": "demo", "status": "succeeded"})
    return settings, db


def test_local_preflight_reports_missing_files_without_changing_data(demo_data):
    from scripts.local_dev import check_local_data

    settings, db = demo_data
    before = {name: list(db[name].find()) for name in db.list_collection_names()}
    files_before = sorted(settings.storage_path.rglob("*"))
    report = check_local_data(settings, db)
    assert report["assets"] == 2
    assert report["brands"] == 1
    assert report["whole_tasks"] == 1
    assert report["missing_assets"] == 1
    assert report["warnings"]
    assert {name: list(db[name].find()) for name in db.list_collection_names()} == before
    assert sorted(settings.storage_path.rglob("*")) == files_before


@pytest.mark.parametrize("collection,document", [
    ("whole_tasks", {"status": "running"}),
    ("whole_tasks", {"status": "succeeded", "edit": {"status": "running"}}),
    ("poster_edit_tasks", {"status": "queued"}),
    ("background_replace_tasks", {"whole_task_id": "task", "status": "running"}),
    ("agent_test_runs", {"status": "preparing"}),
])
def test_local_preflight_refuses_to_interrupt_active_jobs_from_any_merchant(demo_data, collection, document):
    from scripts.local_dev import LocalDevError, check_local_data

    settings, db = demo_data
    db[collection].insert_one({"_id": "active", "merchant_id": "other", **document})
    with pytest.raises(LocalDevError, match="运行中"):
        check_local_data(settings, db)
    assert db[collection].find_one({"_id": "active"})["status"] == document["status"]


def test_explicit_recovery_allows_stale_jobs_without_modifying_them_during_preflight(demo_data):
    from scripts.local_dev import check_local_data

    settings, db = demo_data
    db.whole_tasks.insert_one({"_id": "stale", "merchant_id": "demo", "status": "running"})
    report = check_local_data(settings, db, recover_interrupted=True)
    assert any("恢复" in warning for warning in report["warnings"])
    assert db.whole_tasks.find_one({"_id": "stale"})["status"] == "running"


def test_local_preflight_requires_existing_merchant_and_media_directory(demo_data):
    from scripts.local_dev import LocalDevError, check_local_data

    settings, db = demo_data
    settings.am_bypass_merchant_id = "nonexistent"
    with pytest.raises(LocalDevError, match="商户"):
        check_local_data(settings, db)
    settings.am_bypass_merchant_id = "demo"
    settings.storage_dir = str(settings.storage_path / "nonexistent")
    with pytest.raises(LocalDevError, match="STORAGE_DIR"):
        check_local_data(settings, db)


@pytest.mark.parametrize("values", [
    {"am_bypass_local": False},
    {"social_media_origin": "https://social.example"},
    {"am_bypass_merchant_id": ""},
    {"mongodb_uri": "mongodb://private:secret@remote.example:27017"},
    {"mongodb_uri": "mongodb+srv://private:secret@remote.example"},
])
def test_local_launcher_rejects_nonlocal_or_incomplete_config_without_exposing_credentials(demo_data, values):
    from scripts.local_dev import LocalDevError, validate_local_settings

    settings, _ = demo_data
    for key, value in values.items():
        setattr(settings, key, value)
    with pytest.raises(LocalDevError) as exc:
        validate_local_settings(settings)
    assert "secret" not in str(exc.value)


def test_local_preflight_refuses_media_keys_outside_objects_directory(demo_data):
    from scripts.local_dev import check_local_data

    settings, db = demo_data
    outside = settings.storage_path / "outside.png"
    outside.write_bytes(b"outside")
    db.assets.insert_one({"_id": "unsafe", "merchant_id": "demo", "path": "../outside.png"})
    assert check_local_data(settings, db)["missing_assets"] == 2


def test_busy_port_is_rejected_instead_of_silently_using_another_port():
    import socket
    from scripts.local_dev import LocalDevError, require_free_ports

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(LocalDevError, match=str(port)):
            require_free_ports(port)


@pytest.mark.parametrize("cached", [True, False])
def test_local_am_references_use_only_cached_assets(demo_data, cached):
    from fastapi import HTTPException
    from app.services.request_assets import resolve_asset_id
    from app.services.scoped_db import ScopedDatabase

    settings, db = demo_data
    db.assets.insert_one({"_id": "foreign", "merchant_id": "other", "source": "am", "am_media_library_id": "remote-id"})
    if cached:
        db.assets.insert_one({"_id": "cached", "merchant_id": "demo", "source": "am", "am_media_library_id": "remote-id"})
    class ExplodingAM:
        def get_media(self, *args):
            pytest.fail("Local mode attempted to fetch an AM asset")

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=settings, am=ExplodingAM())),
        state=SimpleNamespace(db=ScopedDatabase(db, "demo"), store=object(), am_cookie="", merchant_id="demo"),
    )
    if cached:
        assert resolve_asset_id(request, "am:remote-id") == "cached"
    else:
        with pytest.raises(HTTPException) as exc:
            resolve_asset_id(request, "am:remote-id")
        assert exc.value.status_code == 422
        assert "本地" in exc.value.detail
