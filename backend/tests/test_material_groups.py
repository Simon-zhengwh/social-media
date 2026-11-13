from __future__ import annotations

import io
from pathlib import Path

import mongomock
import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.services import material_filters as store
from app.services.scoped_db import ScopedDatabase


def jpeg(shift=0, color="red"):
    image=Image.new("RGB",(160,120),"white")
    draw=ImageDraw.Draw(image)
    draw.rectangle((30+shift,25,110+shift,85),fill=color)
    draw.ellipse((55,40,75,60),fill="black")
    buffer=io.BytesIO();image.save(buffer,format="JPEG",quality=90)
    return buffer.getvalue()


@pytest.fixture
def env(tmp_path):
    source=tmp_path/"source";source.mkdir()
    raw=mongomock.MongoClient().db
    db=ScopedDatabase(raw,"m1")
    settings=Settings(storage_dir=str(tmp_path/"store"),core_ai_api_key="test-key",material_filter_concurrency=2)
    db.material_filter_folders.insert_one({"_id":"folder","source_root":str(source),"canonical_source_key":"test-source"})
    menu={"merchant_id":"m1","menu_version":"v1","name_format":"en","items":[{"id":"dish-1","zh":None,"en":"Test Dish"},{"id":"dish-2","zh":None,"en":"Other Dish"}]}
    scheme=store.create_scheme(db,settings,store.get_folder(db,"folder"),name="命名",naming_mode="menu_match",menu=menu,recursive=True)
    return db,settings,source,scheme,raw


def populate(source,count=10):
    data=jpeg()
    for index in range(count):
        (source/f"{index:02}.jpg").write_bytes(data)


def test_local_scan_creates_all_members_and_one_group_without_model_calls(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env
    populate(source)
    before={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in source.iterdir()}
    stats=groups.scan_scheme(db,settings,scheme)
    assert stats["files"]==10 and stats["groups"]==1, stats
    page=groups.list_groups(db,settings,scheme)
    assert page["stats"]["new_calls"]==1
    assert page["stats"]["representatives"]==1
    members=groups.list_members(db,settings,page["items"][0]["id"])
    assert members["total"]==10
    assert all(row["evaluation"] is None and row["recognition_status"]=="unrecognized" for row in members["items"])
    assert before=={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in source.iterdir()}


def test_group_members_have_independent_ids_across_subfolders_and_scopes(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,raw=env
    for folder in ["a","b"]:
        (source/folder).mkdir();(source/folder/"same.jpg").write_bytes(jpeg())
    groups.scan_scheme(db,settings,scheme)
    page=groups.list_groups(db,settings,scheme)
    members=groups.list_members(db,settings,page["items"][0]["id"])
    assert {row["relative_path"] for row in members["items"]}=={"a/same.jpg","b/same.jpg"}
    assert len({row["id"] for row in members["items"]})==2
    with pytest.raises(store.MaterialFilterError):
        groups.get_group(ScopedDatabase(raw,"another-merchant"),page["items"][0]["id"])


def test_incremental_scan_reuses_preview_features_without_reading_originals(env,monkeypatch):
    from app.services import material_groups as groups
    from app.services import material_filter_files as files
    db,settings,source,scheme,_=env;populate(source,3)
    groups.scan_scheme(db,settings,scheme)
    monkeypatch.setattr(files,"read_source_snapshot",lambda *args,**kwargs:pytest.fail("unchanged source must not be re-read"))
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    assert groups.list_groups(db,settings,store.get_scheme(db,scheme["_id"]))["stats"]["files"]==3


def test_feature_version_upgrade_does_not_replace_in_use_local_previews(env,monkeypatch):
    from app.services import material_groups as groups
    from app.services import material_filter_files as files
    db,settings,source,scheme,_=env;populate(source,2)
    groups.scan_scheme(db,settings,scheme)
    db[store.FILE_COLLECTION].update_many({"scheme_id":scheme["_id"]},{"$set":{"features.version":"old-version"}})
    monkeypatch.setattr(files,"read_source_snapshot",lambda *args,**kwargs:pytest.fail("feature upgrade must use local preview"))
    monkeypatch.setattr(files,"_atomic_write",lambda *args,**kwargs:(_ for _ in ()).throw(PermissionError("preview being served")))
    result=groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    assert result["files"]==2 and not result["errors"]


def test_representative_changes_validate_membership_count_and_version(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env;populate(source,3)
    groups.scan_scheme(db,settings,scheme)
    group=groups.list_groups(db,settings,scheme)["items"][0]
    members=groups.list_members(db,settings,group["id"])["items"]
    for ids,version in [(["foreign"],group["version"]),([m["id"] for m in members],group["version"]),([members[0]["id"]],"stale")]:
        with pytest.raises(store.MaterialFilterError):
            groups.set_representatives(db,settings,group["id"],ids,version=version)
    updated=groups.set_representatives(db,settings,group["id"],[members[-1]["id"]],version=group["version"])
    assert updated["representative_file_ids"]==[members[-1]["id"]]


def test_manual_representatives_respect_count_and_reject_exact_duplicates(env):
    from app.services import material_groups as groups
    assert groups.representatives({"manual_representatives":True,"representative_file_ids":["a","b"]},1)==["a"]
    db,settings,source,scheme,_=env;populate(source,2)
    groups.scan_scheme(db,settings,scheme)
    group=groups.list_groups(db,settings,scheme)["items"][0]
    members=groups.list_members(db,settings,group["id"])["items"]
    with pytest.raises(store.MaterialFilterError,match="相同"):
        groups.set_representatives(db,settings,group["id"],[m["id"] for m in members],version=group["version"])


def test_cache_keeps_all_source_versions(env):
    from app.services import material_groups as groups
    db,_,_,scheme,_=env
    common={"scheme_id":scheme["_id"],"file_id":"f","decision":"pass","model":scheme["model"],"prompt_version":scheme["prompt_version"],"naming":{"menu_version":"v1"}}
    for sha in ["A","B"]:
        db[store.EVALUATION_COLLECTION].insert_one(dict(common,_id=sha,source_sha256=sha))
    cache=groups.cached_evaluations(db,scheme)
    assert groups.valid_evaluation({"_id":"f","sha256":"A"},cache)["_id"]=="A"
    assert groups.valid_evaluation({"_id":"f","sha256":"B"},cache)["_id"]=="B"


def test_attempt_timestamps_explicitly_mark_utc():
    from app.services import material_groups as groups
    value=groups.attempt_out({"_id":"a","created_at":store.now(),"called_at":store.now(),"finished_at":store.now()})
    assert all(value[key].endswith("Z") for key in ["created_at","called_at","finished_at"])


def test_bad_images_remain_in_inventory_and_error_list(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env;populate(source,1)
    (source/"bad.jpg").write_bytes(b"not an image")
    result=groups.scan_scheme(db,settings,scheme)
    page=groups.list_groups(db,settings,scheme)
    assert result["files"]==2 and page["stats"]["files"]==2
    assert db[store.FILE_COLLECTION].count_documents({})==2
    assert page["scan_errors"][0]["relative_path"]=="bad.jpg"
    bad=groups.get_file(db,page["scan_errors"][0]["file_id"])
    assert groups.file_out(db,settings,scheme,bad)["recognition_status"]=="error"


def test_split_is_local_persistent_and_versioned(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env;populate(source,3)
    groups.scan_scheme(db,settings,scheme)
    old=groups.list_groups(db,settings,scheme)["items"][0]
    ids=[m["id"] for m in groups.list_members(db,settings,old["id"])["items"]]
    before={p.name:p.read_bytes() for p in source.iterdir()}
    with pytest.raises(store.MaterialFilterError):
        groups.split_group(db,settings,old["id"],ids,version=old["version"])
    groups.split_group(db,settings,old["id"],ids[:1],version=old["version"])
    page=groups.list_groups(db,settings,scheme)
    assert sorted(g["count"] for g in page["items"])==[1,2]
    with pytest.raises(store.MaterialFilterError):groups.get_group(db,old["id"])
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    assert sorted(g["count"] for g in groups.list_groups(db,settings,scheme)["items"])==[1,2]
    assert before=={p.name:p.read_bytes() for p in source.iterdir()}


def test_group_edits_obey_scheme_lease(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env;populate(source,2)
    groups.scan_scheme(db,settings,scheme)
    group=groups.list_groups(db,settings,scheme)["items"][0]
    fid=groups.list_members(db,settings,group["id"])["items"][0]["id"]
    run,_=store.create_run(db,scheme,request_id="busy")
    with pytest.raises(store.MaterialFilterError):
        groups.split_group(db,settings,group["id"],[fid],version=group["version"])
    assert store.get_scheme(db,scheme["_id"])["active_run_id"]==run["_id"]


def test_incremental_scan_deduplicates_manual_representatives(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env
    (source/"a.jpg").write_bytes(jpeg())
    (source/"b.jpg").write_bytes(jpeg(shift=1))
    groups.scan_scheme(db,settings,scheme)
    group=groups.list_groups(db,settings,scheme)["items"][0]
    members=groups.list_members(db,settings,group["id"])["items"]
    groups.set_representatives(db,settings,group["id"],[m["id"] for m in members],version=group["version"])
    (source/"b.jpg").write_bytes(jpeg())
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    assert groups.list_groups(db,settings,scheme,representative_count=2)["stats"]["representatives"]==1


def test_local_match_cache_upgrade_uses_only_existing_local_previews(env,monkeypatch):
    from app.services import material_groups as groups
    from app.services import material_filter_files as files
    from app.services import material_local_match as local
    db,settings,source,scheme,_=env;populate(source,2)
    groups.scan_scheme(db,settings,scheme)
    monkeypatch.setattr(local,"VERSION","future-local-version")
    monkeypatch.setattr(files,"read_source_snapshot",lambda *args,**kwargs:pytest.fail("unchanged source must not be re-read"))
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    for row in db[store.FILE_COLLECTION].find({}):
        assert row["local_match_version"]=="future-local-version"
        assert "alignment" not in row["features"]
        assert (store.scheme_directory(settings,scheme)/row["local_match_path"]).is_file()


def test_failed_local_write_cannot_reuse_previous_source_alignment_on_recovery(env,monkeypatch):
    import json
    from app.services import material_groups as groups
    from app.services import material_filter_files as files
    db,settings,source,scheme,_=env;populate(source,1)
    groups.scan_scheme(db,settings,scheme)
    old=db[store.FILE_COLLECTION].find_one({})
    (source/"00.jpg").write_bytes(jpeg(color="blue"))
    original=files._atomic_write
    def fail_write(*args,**kwargs):raise OSError("simulated local write failure")
    monkeypatch.setattr(files,"_atomic_write",fail_write)
    assert len(groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))["errors"])==1
    failed=db[store.FILE_COLLECTION].find_one({})
    assert not failed.get("local_match_path")
    monkeypatch.setattr(files,"_atomic_write",original)
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    recovered=db[store.FILE_COLLECTION].find_one({})
    assert recovered["sha256"]!=old["sha256"]
    assert recovered["local_match_path"]!=old["local_match_path"]
    cache=json.loads((store.scheme_directory(settings,scheme)/recovered["local_match_path"]).read_text())
    assert cache["source_sha256"]==recovered["sha256"]
