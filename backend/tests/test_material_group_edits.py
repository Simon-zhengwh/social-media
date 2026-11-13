from __future__ import annotations

import pytest

from app.services import material_filters as store
from app.services import material_groups as groups
from app.services import material_group_runner as runner
from tests.test_material_groups import env, jpeg, populate


def separated(env):
    db,settings,source,scheme,_=env
    (source/"a.jpg").write_bytes(jpeg())
    (source/"b.jpg").write_bytes(jpeg(color="blue"))
    groups.scan_scheme(db,settings,scheme)
    page=groups.list_groups(db,settings,scheme)
    assert page["total"]==2
    return db,settings,source,scheme,page


def merge(db,settings,scheme,page,request_id="merge-1"):
    return groups.merge_groups(db,settings,scheme["_id"],[g["id"] for g in page["items"]],version=page["items"][0]["version"],request_id=request_id)


def test_filter_schemes_can_group_locally_but_cannot_sample(env):
    db,settings,source,_,_=env;populate(source,3)
    scheme=store.create_scheme(db,settings,store.get_folder(db,"folder"),name="过滤",rules=[{"id":"empty_seating_scene","text":"不能出现空座位","interpretation":"空座位场景不通过"}])
    groups.scan_scheme(db,settings,scheme)
    file=db[store.FILE_COLLECTION].find_one({"scheme_id":scheme["_id"]})
    db[store.EVALUATION_COLLECTION].insert_one({"_id":"existing","scheme_id":scheme["_id"],"file_id":file["_id"],"source_sha256":file["sha256"],
        "model":scheme["model"],"prompt_version":scheme["prompt_version"],"decision":"fail","effective_decision":"fail","display_name":"保留原判断"})
    page=groups.list_groups(db,settings,scheme)
    assert page["sampling_enabled"] is False and page["stats"]["new_calls"]==0
    assert page["stats"]["recognized"]==1
    members=groups.list_members(db,settings,page["items"][0]["id"])["items"]
    assert sum(m["evaluation"] is not None for m in members)==1
    assert next(m["evaluation"] for m in members if m["evaluation"])["effective_decision"]=="fail"
    with pytest.raises(store.MaterialFilterError):runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),request_id="forbidden")
    assert db[groups.ATTEMPTS].count_documents({})==0


def test_merge_is_local_idempotent_and_survives_incremental_scan(env):
    db,settings,source,scheme,page=separated(env)
    before={p.name:p.read_bytes() for p in source.iterdir()}
    result=merge(db,settings,scheme,page)
    assert result["group"]["count"]==2 and result["group"]["manual"]
    again=merge(db,settings,scheme,page)
    assert again["group"]["id"]==result["group"]["id"]
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    current=groups.list_groups(db,settings,scheme)
    assert current["total"]==1 and current["items"][0]["manual"]
    assert before=={p.name:p.read_bytes() for p in source.iterdir()}
    assert db[groups.ATTEMPTS].count_documents({})==0


def test_changed_source_is_not_forced_into_an_old_manual_group(env):
    db,settings,source,scheme,page=separated(env)
    merge(db,settings,scheme,page)
    (source/"b.jpg").write_bytes(jpeg(color="green"))
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    assert groups.list_groups(db,settings,scheme)["total"]==2


def test_splitting_a_manual_group_stays_split_after_scan(env):
    db,settings,source,scheme,page=separated(env)
    merged=merge(db,settings,scheme,page)["group"]
    member=groups.list_members(db,settings,merged["id"])["items"][0]
    groups.split_group(db,settings,merged["id"],[member["id"]],version=merged["version"])
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    assert groups.list_groups(db,settings,scheme)["total"]==2


def test_merge_validates_version_scope_selection_and_operation_id(env):
    db,settings,source,scheme,page=separated(env)
    ids=[g["id"] for g in page["items"]];version=page["items"][0]["version"]
    for chosen,v in [([ids[0]],version),([ids[0],ids[0]],version),(ids,"stale"),([ids[0],"foreign"],version)]:
        with pytest.raises(store.MaterialFilterError):groups.merge_groups(db,settings,scheme["_id"],chosen,version=v,request_id="bad-"+v+str(chosen))
    result=merge(db,settings,scheme,page)
    with pytest.raises(store.MaterialFilterError):groups.merge_groups(db,settings,scheme["_id"],list(reversed(ids)),version=version,request_id="merge-1-changed")
    with pytest.raises(store.MaterialFilterError):groups.merge_groups(db,settings,scheme["_id"],ids,version="different",request_id="merge-1")
    assert groups.list_groups(db,settings,scheme)["total"]==1


def test_group_preview_strip_is_bounded_and_includes_current_representative(env):
    db,settings,source,scheme,_=env;populate(source,12)
    groups.scan_scheme(db,settings,scheme)
    g=groups.list_groups(db,settings,scheme)["items"][0]
    members=groups.list_members(db,settings,g["id"])["items"]
    groups.set_representatives(db,settings,g["id"],[members[-1]["id"]],version=g["version"])
    g=groups.list_groups(db,settings,scheme)["items"][0]
    assert len(g["previews"])==8 and g["previews"][0]["id"]==members[-1]["id"]
    assert len({p["id"] for p in g["previews"]})==8
    assert all("features" not in p and "evaluation" not in p for p in g["previews"])


def test_failed_merge_keeps_published_groups_usable_for_recognition(env,monkeypatch):
    db,settings,source,scheme,page=separated(env)
    before={r["_id"]:(r["group_id"],r["group_revision"]) for r in db[store.FILE_COLLECTION].find({})}
    def fail(*args,**kwargs):raise OSError("simulated staging failure")
    monkeypatch.setattr(groups,"_refresh_reference",fail)
    with pytest.raises(OSError):merge(db,settings,scheme,page)
    assert store.get_scheme(db,scheme["_id"])["active_run_id"] is None
    assert before=={r["_id"]:(r["group_id"],r["group_revision"]) for r in db[store.FILE_COLLECTION].find({})}
    run,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),request_id="after-failed-merge")
    assert len(run["files"])==2


def test_group_edit_kind_is_persisted_before_any_post_creation_step(env,monkeypatch):
    db,settings,source,scheme,page=separated(env)
    original=store.create_run
    def crash(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError("process stopped after initial run insert")
    monkeypatch.setattr(store,"create_run",crash)
    with pytest.raises(OSError):merge(db,settings,scheme,page)
    row=db[store.RUN_COLLECTION].find_one({"request_id":"group-merge:merge-1"})
    assert row["kind"]=="group_edit"
    store.mark_scheme_interrupted(db)
    assert store.get_run(db,row["_id"])["status"]=="failed"


def test_recognition_uses_published_membership_when_file_pointer_cache_lags(env):
    db,settings,source,scheme,page=separated(env)
    merged=merge(db,settings,scheme,page)["group"]
    db[store.FILE_COLLECTION].update_many({"scheme_id":scheme["_id"]},{"$set":{"group_id":"stale-pointer","group_revision":"stale-revision"}})
    ids=[r["_id"] for r in db[store.FILE_COLLECTION].find({"scheme_id":scheme["_id"]})]
    run,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),request_id="lagging-pointer",mode="selected_files",file_ids=ids)
    assert len(run["files"])==2 and all(p["group_id"]==merged["id"] for p in run["files"])


def _add_result(db,scheme,file_row,name="Test Dish"):
    row={"_id":"eval-"+file_row["_id"],"scheme_id":scheme["_id"],"file_id":file_row["_id"],"source_sha256":file_row["sha256"],
        "model":scheme["model"],"prompt_version":scheme.get("prompt_version"),"naming_mode":"menu_match",
        "naming":{"menu_version":(scheme.get("menu") or {}).get("menu_version")},
        "decision":"pass","effective_decision":"pass","display_name":name,"tags":["菜品"],
        "manual_pick":True,"picked_at":store.now(),"copy_status":"copied"}
    db[store.EVALUATION_COLLECTION].insert_one(row)
    return row


def test_manual_group_name_is_editable_and_becomes_the_picked_file_name(env):
    from pathlib import Path
    db,settings,source,scheme,page=separated(env)
    group=page["items"][0]
    before={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in source.iterdir()}
    renamed=groups.rename_group(db,settings,group["id"],"  卤牛腱肉_Spiced Beef  ",version=group["version"])
    assert renamed["reference_name"]=="卤牛腱肉_Spiced Beef" and renamed["name_manual"] and renamed["status"]=="manual"
    assert before=={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in source.iterdir()}
    assert db[store.EVALUATION_COLLECTION].count_documents({})==0

    file_row=db[store.FILE_COLLECTION].find_one({"scheme_id":scheme["_id"],"group_id":group["id"]})
    _add_result(db,scheme,file_row)
    directory=store.scheme_directory(settings,store.get_scheme(db,scheme["_id"]))
    passed=directory/"passed";passed.mkdir(parents=True,exist_ok=True)
    (passed/"Test Dish__old.jpg").write_bytes(jpeg())
    db[store.EVALUATION_COLLECTION].update_one({"_id":"eval-"+file_row["_id"]},{"$set":{"output_path":"passed/Test Dish__old.jpg"}})

    exported=store.export_picked(db,settings,store.get_scheme(db,scheme["_id"]))
    names={p.name for p in Path(exported["directory"]).rglob("*.jpg")}
    assert names=={"卤牛腱肉_Spiced Beef.jpg"}
    manifest=(Path(exported["directory"])/"manifest.csv").read_text(encoding="utf-8-sig")
    assert "卤牛腱肉_Spiced Beef" in manifest


def test_manual_group_name_survives_rescan_and_later_representative_results(env):
    db,settings,source,scheme,page=separated(env)
    group=page["items"][0]
    groups.rename_group(db,settings,group["id"],"卤牛腱肉_Spiced Beef",version=group["version"])
    file_row=db[store.FILE_COLLECTION].find_one({"scheme_id":scheme["_id"],"group_id":group["id"]})
    _add_result(db,scheme,file_row,name="另一种名字")
    groups._refresh_reference(db,groups.get_group(db,group["id"]))
    current=groups.get_group(db,group["id"])
    assert current["reference_name"]=="卤牛腱肉_Spiced Beef" and current["status"]=="manual"

    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    revision=store.get_scheme(db,scheme["_id"])["group_revision"]
    row=db[groups.COLLECTION].find_one({"version":revision,"file_ids":file_row["_id"]})
    assert row["reference_name"]=="卤牛腱肉_Spiced Beef" and row["name_manual"]
    rescanned=next(g for g in groups.list_groups(db,settings,scheme)["items"] if g["id"]==row["_id"])
    assert rescanned["reference_name"]=="卤牛腱肉_Spiced Beef" and rescanned["name_manual"]


def test_group_name_validation_and_stale_versions(env):
    db,settings,source,scheme,page=separated(env)
    group=page["items"][0]
    for name in ["", "   ", "长"*61]:
        with pytest.raises(store.MaterialFilterError):groups.rename_group(db,settings,group["id"],name,version=group["version"])
    with pytest.raises(store.MaterialFilterError):groups.rename_group(db,settings,group["id"],"名称",version="stale-version")
    assert groups.get_group(db,group["id"])["reference_name"] is None


def test_picked_previews_lead_the_group_strip_and_are_flagged(env):
    db,settings,source,scheme,page=separated(env)
    group=page["items"][0]
    file_row=db[store.FILE_COLLECTION].find_one({"scheme_id":scheme["_id"],"group_id":group["id"]})
    _add_result(db,scheme,file_row)
    item=next(g for g in groups.list_groups(db,settings,scheme)["items"] if g["id"]==group["id"])
    assert item["previews"][0]["id"]==file_row["_id"] and item["previews"][0]["manual_pick"] is True
    assert item["picked_file_ids"]==[file_row["_id"]] and item["picked_count"]==1
