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


def test_backfill_picks_first_two_members_from_an_existing_successful_group(env):
    db,settings,source,scheme,_=env
    populate(source,3)
    groups.scan_scheme(db,settings,scheme)

    class MatchedClient:
        def chat_json(self,*args,**kwargs):
            return {"naming_status":"matched","selected_menu_id":"dish-1","candidate_menu_ids":["dish-1"],
                "visual_subject":"菜品","reason":"菜单菜品特征相符","uncertain_attributes":[],"category":"食物","tags":["食材","蔬菜"]}

    run,_=runner.create_recognition_run(db,settings,scheme,request_id="existing-success",mode="representatives")
    runner.run_job(db,settings,MatchedClient(),run["_id"])
    group=groups.list_groups(db,settings,scheme)["items"][0]
    members=groups.list_members(db,settings,group["id"])["items"]
    representative=next(item for item in members if item["evaluation"])
    store.update_evaluation(db,representative["evaluation"]["_id"],{"manual_pick":False,"picked_at":None})

    result=groups.backfill_picks_for_passed_groups(db,settings,store.get_scheme(db,scheme["_id"]))
    assert result["groups"]==1 and result["photos_picked"]==2
    refreshed=groups.list_members(db,settings,group["id"])["items"]
    chosen=refreshed[:2]
    assert all(item["evaluation"] and item["evaluation"]["manual_pick"] for item in chosen)
    assert all(item["evaluation"]["display_name"]=="Test Dish" for item in chosen)
    second_evaluation = db[store.EVALUATION_COLLECTION].find_one({"_id": chosen[1]["evaluation"]["_id"]})
    store.set_manual_pick(db, second_evaluation, picked=False)
    repeated=groups.backfill_picks_for_passed_groups(db,settings,store.get_scheme(db,scheme["_id"]))
    assert repeated["photos_picked"]==1
    refreshed=groups.list_members(db,settings,group["id"])["items"]
    assert refreshed[0]["evaluation"]["manual_pick"] is True
    assert refreshed[1]["evaluation"]["manual_pick"] is False
    assert db[store.EVALUATION_COLLECTION].count_documents({"scheme_id":scheme["_id"],"manual_pick":True})==1
    store.update_evaluation(db,representative["evaluation"]["_id"],{
        "decision":"uncertain","model_decision":"uncertain","effective_decision":"uncertain"})
    groups.backfill_picks_for_passed_groups(db,settings,store.get_scheme(db,scheme["_id"]))
    stale=groups.list_members(db,settings,group["id"])["items"][:2]
    # 待复核 groups stay shortlisted for upload; the operator's unpick survives.
    assert all(item["evaluation"]["decision"]=="uncertain" for item in stale)
    assert all(item["evaluation"]["effective_decision"]=="uncertain" for item in stale)
    assert [item["evaluation"]["manual_pick"] for item in stale]==[True,False]


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


class _MatchedClient:
    def chat_json(self,*args,**kwargs):
        return {"naming_status":"matched","selected_menu_id":"dish-1","candidate_menu_ids":["dish-1"],
            "visual_subject":"菜品","reason":"菜单菜品特征相符","uncertain_attributes":[],"category":"食物","tags":["食材","蔬菜"]}


def test_recognition_run_shortlists_groups_left_over_from_earlier_runs(env):
    """A newer run must heal passed groups whose picks predate the shortlist step."""
    db,settings,source,scheme,_=separated(env)
    first,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),
                                          request_id="first-pass",mode="representatives")
    runner.run_job(db,settings,_MatchedClient(),first["_id"])
    ordered=list(db[groups.COLLECTION].find({"scheme_id":scheme["_id"]}).sort("_id",1))
    assert len(ordered)==2
    older=db[store.EVALUATION_COLLECTION].find_one({"scheme_id":scheme["_id"],"file_id":ordered[1]["file_ids"][0]})
    store.update_evaluation(db,older["_id"],{"manual_pick":False,"picked_at":None})
    db[groups.COLLECTION].update_many({"scheme_id":scheme["_id"]},{"$unset":{"auto_picked":""}})
    assert groups.list_groups(db,settings,store.get_scheme(db,scheme["_id"]))["stats"]["pending_picks"]==1

    touched=ordered[0]["file_ids"][0]
    second,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),
                                            request_id="touch-one-group",mode="selected_files",file_ids=[touched])
    runner.run_job(db,settings,_MatchedClient(),second["_id"])

    assert second["group_ids"]==[ordered[0]["_id"]]
    assert db[store.EVALUATION_COLLECTION].find_one({"_id":older["_id"]})["manual_pick"] is True
    assert groups.list_groups(db,settings,store.get_scheme(db,scheme["_id"]))["stats"]["pending_picks"]==0


def test_auto_pick_sweep_claims_one_background_job_per_grouping_revision(env,monkeypatch):
    db,settings,source,scheme,page=separated(env)

    class InlineThread:
        def __init__(self,target=None,args=(),kwargs=None,name=None,daemon=None):
            self.target,self.args=target,args or ()
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(groups,"Thread",InlineThread)
    assert groups.start_auto_pick(db,settings,store.get_scheme(db,scheme["_id"])) is True
    current=store.get_scheme(db,scheme["_id"])
    assert current["auto_pick_revision"]==current["group_revision"] and current["active_run_id"] is None
    run=db[store.RUN_COLLECTION].find_one({"scheme_id":scheme["_id"],"kind":"auto_pick"})
    assert run["status"]=="succeeded" and run["kind"]=="auto_pick"
    assert groups.start_auto_pick(db,settings,current) is False
    assert db[store.RUN_COLLECTION].count_documents({"scheme_id":scheme["_id"],"kind":"auto_pick"})==1


def test_auto_pick_sweep_runs_one_batch_per_page_load_until_done(env,monkeypatch):
    db,settings,source,scheme,_=separated(env)

    class InlineThread:
        def __init__(self,target=None,args=(),kwargs=None,name=None,daemon=None):
            self.target,self.args=target,args or ()
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(groups,"Thread",InlineThread)
    monkeypatch.setattr(groups,"AUTO_PICK_BATCH_GROUPS",1)
    first,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),
                                          request_id="batch-pass",mode="representatives")
    runner.run_job(db,settings,_MatchedClient(),first["_id"])
    assert db[store.EVALUATION_COLLECTION].count_documents({"scheme_id":scheme["_id"],"manual_pick":True})==1
    assert store.get_scheme(db,scheme["_id"])["auto_pick_revision"] is None
    assert groups.list_groups(db,settings,store.get_scheme(db,scheme["_id"]))["stats"]["pending_picks"]==1

    assert groups.start_auto_pick(db,settings,store.get_scheme(db,scheme["_id"])) is True
    assert db[store.EVALUATION_COLLECTION].count_documents({"scheme_id":scheme["_id"],"manual_pick":True})==2
    current=store.get_scheme(db,scheme["_id"])
    assert current["auto_pick_revision"]==current["group_revision"]
    assert groups.list_groups(db,settings,store.get_scheme(db,scheme["_id"]))["stats"]["pending_picks"]==0
    assert groups.start_auto_pick(db,settings,store.get_scheme(db,scheme["_id"])) is False


def test_auto_pick_does_not_override_an_operator_unpick(env,monkeypatch):
    db,settings,source,scheme,_=separated(env)

    class InlineThread:
        def __init__(self,target=None,args=(),kwargs=None,name=None,daemon=None):
            self.target,self.args=target,args or ()
        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(groups,"Thread",InlineThread)
    first,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),
                                          request_id="operator-unpick",mode="representatives")
    runner.run_job(db,settings,_MatchedClient(),first["_id"])
    picked=db[store.EVALUATION_COLLECTION].find_one({"scheme_id":scheme["_id"],"manual_pick":True})
    store.update_evaluation(db,picked["_id"],{"manual_pick":False,"picked_at":None})
    assert groups.list_groups(db,settings,store.get_scheme(db,scheme["_id"]))["stats"]["pending_picks"]==0
    assert groups.start_auto_pick(db,settings,store.get_scheme(db,scheme["_id"])) is False
    assert groups.backfill_picks_for_passed_groups(db,settings,store.get_scheme(db,scheme["_id"]))["photos_picked"]==1


def test_group_browser_filters_groups_by_representative_verdict_and_picks(env):
    db,settings,source,scheme,_=env
    for name,color in [("a.jpg","red"),("b.jpg","blue"),("c.jpg","green")]:
        (source/name).write_bytes(jpeg(color=color))
    groups.scan_scheme(db,settings,scheme)
    page=groups.list_groups(db,settings,scheme)
    assert page["total"]==3
    assert page["counts"]=={"all":3,"unrecognized":3,"pass":0,"fail":0,"uncertain":0,"error":0,"picked":0}
    ordered=sorted(page["items"],key=lambda group:group["id"])
    passing,failing,silent=ordered

    def representative(group):
        return db[store.FILE_COLLECTION].find_one({"_id":group["representative_file_ids"][0]})

    passed=_add_result(db,scheme,representative(passing))
    failed=_add_result(db,scheme,representative(failing))
    store.update_evaluation(db,passed["_id"],{"decision":"pass","effective_decision":"pass","manual_pick":True})
    store.update_evaluation(db,failed["_id"],{"decision":"fail","effective_decision":"fail","manual_pick":False})
    silent_file=representative(silent)
    db[groups.ATTEMPTS].insert_one({"_id":"attempt-silent","scheme_id":scheme["_id"],"file_id":silent_file["_id"],
        "source_sha256":silent_file["sha256"],"model":scheme["model"],"prompt_version":scheme.get("prompt_version"),
        "menu_version":(scheme.get("menu") or {}).get("menu_version"),"status":"failed","error":"超时","created_at":store.now()})

    page=groups.list_groups(db,settings,scheme)
    assert page["counts"]=={"all":3,"unrecognized":0,"pass":1,"fail":1,"uncertain":0,"error":1,"picked":1}
    assert groups.list_groups(db,settings,scheme,status="pass")["items"][0]["id"]==passing["id"]
    assert groups.list_groups(db,settings,scheme,status="fail")["items"][0]["id"]==failing["id"]
    assert groups.list_groups(db,settings,scheme,status="error")["items"][0]["id"]==silent["id"]
    assert groups.list_groups(db,settings,scheme,status="picked")["items"][0]["id"]==passing["id"]
    assert groups.list_groups(db,settings,scheme,status="uncertain")["total"]==0
    filtered=groups.list_groups(db,settings,scheme,status="pass")
    assert filtered["counts"]["all"]==3 and filtered["stats"]["groups"]==3
    first_file=db[groups.COLLECTION].find_one({"_id":passing["id"]})["file_ids"][0]
    member=db[store.FILE_COLLECTION].find_one({"_id":first_file})["relative_path"]
    assert groups.list_groups(db,settings,scheme,status="pass",q=member)["total"]==1
    assert groups.list_groups(db,settings,scheme,status="fail",q=member)["total"]==0
    store.update_evaluation(db,failed["_id"],{"effective_decision":"error"})
    assert groups.list_groups(db,settings,scheme)["counts"]=={"all":3,"unrecognized":0,"pass":1,"fail":0,"uncertain":0,"error":2,"picked":1}
    with pytest.raises(store.MaterialFilterError):
        groups.list_groups(db,settings,scheme,status="bogus")


def test_uncertain_representatives_feed_the_group_batch_re_recognition(env):
    db,settings,source,scheme,_=env
    for name,color in [("a.jpg","red"),("b.jpg","blue")]:
        (source/name).write_bytes(jpeg(color=color))
    groups.scan_scheme(db,settings,scheme)
    page=groups.list_groups(db,settings,scheme)
    assert groups.unsettled_representatives(db,scheme)=={"file_ids":[],"total":0}
    for item in page["items"]:
        row=db[store.FILE_COLLECTION].find_one({"_id":item["representative_file_ids"][0]})
        evaluation=_add_result(db,scheme,row)
        store.update_evaluation(db,evaluation["_id"],{"decision":"uncertain","effective_decision":"uncertain",
            "manual_pick":False,"picked_at":None})
    # A non-representative photo conclusion must not add another batch target.
    other=db[store.FILE_COLLECTION].find_one({"scheme_id":scheme["_id"],
        "_id":{"$nin":[item["representative_file_ids"][0] for item in page["items"]]}})
    if other:
        extra=_add_result(db,scheme,other,name="非代表图")
        store.update_evaluation(db,extra["_id"],{"decision":"uncertain","effective_decision":"uncertain","manual_pick":False})
    result=groups.unsettled_representatives(db,scheme)
    assert result["total"]==2 and sorted(result["file_ids"])==sorted(
        item["representative_file_ids"][0] for item in page["items"])
    assert groups.unsettled_representatives(db,scheme,limit=1)=={"file_ids":result["file_ids"][:1],"total":2}


def test_uncertain_group_member_borrows_the_representative_result_when_picked(env):
    db,settings,source,scheme,_=env
    populate(source,3)
    groups.scan_scheme(db,settings,scheme)
    group=groups.list_groups(db,settings,scheme)["items"][0]
    members=groups.list_members(db,settings,group["id"])["items"]
    stray=next(m for m in members if m["id"]!=group["representative_file_ids"][0])
    # Nothing to borrow until the representative has a result.
    with pytest.raises(store.MaterialFilterError):groups.manual_inherit_pick(db,settings,group["id"],stray["id"])
    representative_row=db[store.FILE_COLLECTION].find_one({"_id":group["representative_file_ids"][0]})
    result=_add_result(db,scheme,representative_row)
    naming={"naming_status":"matched","selected_menu_id":"dish-1","candidate_menu_ids":["dish-1"],
        "name_zh":"测试菜","name_en":"Test Dish","visual_subject":"菜品","reason":"菜单菜品特征相符",
        "uncertain_attributes":[],"category":"食物","tags":["食材"],"menu_version":"v1"}
    store.update_evaluation(db,result["_id"],{"naming":naming,"decision":"uncertain","effective_decision":"uncertain",
        "manual_pick":False,"picked_at":None})

    picked=groups.manual_inherit_pick(db,settings,group["id"],stray["id"])
    assert picked["manual_pick"] is True and picked["pick_source"]=="manual"
    assert picked["decision"]=="uncertain" and picked["effective_decision"]=="uncertain"
    assert picked["display_name"]=="Test Dish" and picked["copy_status"]=="copied"
    copy=store.scheme_directory(settings,store.get_scheme(db,scheme["_id"]))/picked["output_path"]
    assert copy.is_file()
    # Repeating the pick keeps the same record instead of copying again.
    assert groups.manual_inherit_pick(db,settings,group["id"],stray["id"])["_id"]==picked["_id"]
    refreshed=next(g for g in groups.list_groups(db,settings,scheme)["items"] if g["id"]==group["id"])
    assert refreshed["picked_count"]==1

    store.update_evaluation(db,result["_id"],{"decision":"fail","effective_decision":"fail"})
    another=next(m for m in members if m["id"] not in {group["representative_file_ids"][0],stray["id"]})
    with pytest.raises(store.MaterialFilterError):groups.manual_inherit_pick(db,settings,group["id"],another["id"])
    with pytest.raises(store.MaterialFilterError):groups.manual_inherit_pick(db,settings,group["id"],"mfile_missing")


def test_uncertain_groups_are_shortlisted_so_they_can_be_uploaded(env):
    db,settings,source,scheme,_=env
    populate(source,4)
    groups.scan_scheme(db,settings,scheme)

    class AmbiguousClient:
        def chat_json(self,*args,**kwargs):
            return {"naming_status":"ambiguous","selected_menu_id":None,"candidate_menu_ids":["dish-1","dish-2"],
                "visual_subject":"抄手或水饺","reason":"两种菜品特征都符合","uncertain_attributes":["菜名"],
                "category":"食物","tags":["食材","红油"]}

    run,_=runner.create_recognition_run(db,settings,store.get_scheme(db,scheme["_id"]),
                                        request_id="uncertain-shortlist",mode="representatives")
    runner.run_job(db,settings,AmbiguousClient(),run["_id"])
    group=groups.list_groups(db,settings,scheme)["items"][0]
    assert group["verdict"]=="uncertain"
    shortlist=groups.list_members(db,settings,group["id"])["items"][:2]
    assert all(item["evaluation"] for item in shortlist)
    assert all(item["evaluation"]["manual_pick"] is True for item in shortlist)
    assert all(item["evaluation"]["decision"]=="uncertain" for item in shortlist)
    assert all(item["evaluation"]["effective_decision"]=="uncertain" for item in shortlist)
    assert all(item["evaluation"]["copy_status"]=="copied" for item in shortlist)
    assert all("菜名待确认" in item["evaluation"]["output_name"] for item in shortlist)
    directory=store.scheme_directory(settings,store.get_scheme(db,scheme["_id"]))
    assert all((directory/item["evaluation"]["output_path"]).is_file() for item in shortlist)
    assert groups.list_groups(db,settings,scheme)["stats"]["pending_picks"]==0

    # A later fail verdict still clears the automatic shortlist.
    representative=next(item for item in groups.list_members(db,settings,group["id"])["items"] if item["evaluation"])
    store.update_evaluation(db,representative["evaluation"]["_id"],
        {"decision":"fail","effective_decision":"fail"})
    groups.auto_pick_successful_group(db,settings,store.get_scheme(db,scheme["_id"]),groups.get_group(db,group["id"]))
    cleared=groups.list_members(db,settings,group["id"])["items"][:2]
    assert all(item["evaluation"]["manual_pick"] is False for item in cleared)


def test_uncertain_groups_marked_by_the_older_policy_are_swept_again(env):
    db,settings,source,scheme,_=env
    populate(source,3)
    groups.scan_scheme(db,settings,scheme)
    group=groups.get_group(db,groups.list_groups(db,settings,scheme)["items"][0]["id"])
    representative=next(row for row in db[store.FILE_COLLECTION].find({"scheme_id":scheme["_id"]}) if row["_id"] in group["representative_file_ids"])
    result=_add_result(db,scheme,representative)
    naming={"naming_status":"ambiguous","selected_menu_id":None,"candidate_menu_ids":["dish-1"],
        "name_zh":None,"name_en":None,"visual_subject":"抄手或水饺","reason":"特征都符合",
        "uncertain_attributes":["菜名"],"category":"食物","tags":["食材"],"menu_version":"v1"}
    store.update_evaluation(db,result["_id"],{"naming":naming,"decision":"uncertain","effective_decision":"uncertain",
        "manual_pick":False,"picked_at":None})
    # An older sweep visited the group while 待复核 groups were ineligible.
    db[groups.COLLECTION].update_one({"_id":group["_id"]},{"$set":{"auto_picked":True}})
    assert groups.list_groups(db,settings,scheme)["stats"]["pending_picks"]==1
    totals=groups.sync_auto_picks(db,settings,store.get_scheme(db,scheme["_id"]))
    assert totals["groups"]==1 and totals["photos_picked"]==2
    assert groups.list_groups(db,settings,scheme)["stats"]["pending_picks"]==0
