from __future__ import annotations

import pytest
from app.services import material_filters as store
from app.services.core_ai import CoreAIError
from tests.test_material_groups import env, jpeg, populate


class FakeClient:
    def __init__(self,response=None):
        self.calls=0;self.response=response

    def chat_json(self,*args,**kwargs):
        self.calls+=1
        if isinstance(self.response,Exception):raise self.response
        return self.response or {"naming_status":"matched","selected_menu_id":"dish-1","candidate_menu_ids":["dish-1"],"visual_subject":"菜品","reason":"可见菜品","uncertain_attributes":[],"category":"食物","tag_groups":{"食材":["蔬菜"]}}


def setup_groups(env):
    from app.services import material_groups as groups
    db,settings,source,scheme,_=env;populate(source)
    groups.scan_scheme(db,settings,scheme)
    return groups,db,settings,source,store.get_scheme(db,scheme["_id"])


def run_recognition(db,settings,scheme,client,request_id,**kwargs):
    from app.services import material_group_runner as runner
    run,created=runner.create_recognition_run(db,settings,scheme,request_id=request_id,**kwargs)
    if created:runner.run_job(db,settings,client,run["_id"])
    return store.get_run(db,run["_id"])


def test_ten_similar_members_use_one_call_and_same_operation_is_idempotent(env):
    groups,db,settings,source,scheme=setup_groups(env)
    client=FakeClient()
    run=run_recognition(db,settings,scheme,client,"operation-1")
    assert run["status"]=="succeeded" and client.calls==1
    assert run["counters"]["model_calls"]==1
    again=run_recognition(db,settings,scheme,client,"operation-1")
    assert again["_id"]==run["_id"] and client.calls==1
    assert db.material_filter_evaluations.count_documents({})==1
    saved = db.material_filter_evaluations.find_one()
    assert saved["tag_groups"] == {"食材": ["蔬菜"]}
    assert saved["tags"] == ["蔬菜"]


def test_new_similar_members_reuse_existing_representative_without_call(env):
    groups,db,settings,source,scheme=setup_groups(env);client=FakeClient()
    run_recognition(db,settings,scheme,client,"first")
    (source/"new.jpg").write_bytes(jpeg())
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    scheme=store.get_scheme(db,scheme["_id"])
    plan=groups.list_groups(db,settings,scheme)
    assert plan["stats"]["files"]==11 and plan["stats"]["new_calls"]==0
    repeated=run_recognition(db,settings,scheme,client,"second")
    assert client.calls==1
    assert repeated["counters"]["processed"]==1
    assert repeated["counters"]["cache_hits"]==1


def test_force_two_selected_images_adds_exactly_two_calls_and_preserves_attempts(env):
    groups,db,settings,source,scheme=setup_groups(env);client=FakeClient()
    run_recognition(db,settings,scheme,client,"initial")
    group=groups.list_groups(db,settings,scheme)["items"][0]
    ids=[row["id"] for row in groups.list_members(db,settings,group["id"])["items"][:2]]
    run=run_recognition(db,settings,scheme,client,"manual-repeat",mode="selected_files",file_ids=ids,force=True)
    assert client.calls==3 and run["counters"]["model_calls"]==2
    run_recognition(db,settings,scheme,client,"manual-repeat",mode="selected_files",file_ids=ids,force=True)
    assert client.calls==3
    assert db.material_filter_attempts.count_documents({"actual_call":True})==3


def test_changed_source_is_not_sent_using_stale_plan(env):
    from app.services import material_group_runner as runner
    groups,db,settings,source,scheme=setup_groups(env);client=FakeClient()
    run,_=runner.create_recognition_run(db,settings,scheme,request_id="stale-source")
    target=groups.get_file(db,run["files"][0]["file_id"])
    (source/target["relative_path"]).write_bytes(jpeg(color="blue"))
    runner.run_job(db,settings,client,run["_id"])
    assert client.calls==0
    assert store.get_run(db,run["_id"])["counters"]["errors"]==1


def test_updated_grouping_policy_requires_rescan_before_new_model_plan(env,monkeypatch):
    from app.services import material_group_runner as runner
    from app.services import material_similarity as similarity
    groups,db,settings,source,scheme=setup_groups(env)
    monkeypatch.setattr(similarity,"GROUPING_VERSION","future-policy")
    with pytest.raises(store.MaterialFilterError,match="分组"):
        runner.create_recognition_run(db,settings,scheme,request_id="old-policy")


def test_old_recognition_plan_cannot_resume_after_groups_are_rebuilt(env):
    from app.services import material_group_runner as runner
    groups,db,settings,source,scheme=setup_groups(env)
    old,_=runner.create_recognition_run(db,settings,scheme,request_id="old-revision")
    store.mark_scheme_interrupted(db)
    groups.scan_scheme(db,settings,store.get_scheme(db,scheme["_id"]))
    with pytest.raises(store.MaterialFilterError,match="分组"):
        runner.resume(db,settings,FakeClient(),old["_id"])


def test_timeout_requires_explicit_retry_not_run_resume(env):
    from app.services import material_group_runner as runner
    groups,db,settings,source,scheme=setup_groups(env)
    client=FakeClient(CoreAIError("gateway_timeout","结果未知",504))
    run=run_recognition(db,settings,scheme,client,"timeout")
    assert client.calls==1
    assert db.material_filter_attempts.find_one({"run_id":run["_id"]})["status"]=="unknown"
    runner.run_job(db,settings,client,run["_id"])
    assert client.calls==1
    member=groups.list_groups(db,settings,scheme)["items"][0]["cover"]
    assert member["recognition_status"]=="unknown" and member["attempt_count"]==1
    assert groups.list_groups(db,settings,scheme)["stats"]["blocked_unknown"]==1
    assert groups.list_groups(db,settings,scheme)["stats"]["new_calls"]==0
    run_recognition(db,settings,scheme,client,"normal-repeat")
    assert client.calls==1
    history=groups.list_attempts(db,member["id"])
    assert history["total"]==2
    assert history["items"][0]["status"]=="unknown"
    run_recognition(db,settings,scheme,client,"explicit-repeat",mode="selected_files",file_ids=[member["id"]],force=True)
    assert client.calls==2


def test_interrupted_inflight_attempt_becomes_unknown_without_replay(env):
    from app.services import material_group_runner as runner
    from app.services import material_groups as groups_module
    groups,db,settings,source,scheme=setup_groups(env);client=FakeClient()
    run,_=runner.create_recognition_run(db,settings,scheme,request_id="crashed-request")
    file_id=run["files"][0]["file_id"]
    db[groups_module.ATTEMPTS].insert_one({"_id":"old-attempt","run_id":run["_id"],"scheme_id":scheme["_id"],"file_id":file_id,"status":"running","actual_call":True})
    runner.run_job(db,settings,client,run["_id"])
    assert client.calls==0
    current=store.get_run(db,run["_id"])
    assert current["counters"]["model_calls"]==1
    assert current["counters"]["processed"]==1
    assert current["counters"]["errors"]==1


def test_restart_then_new_normal_run_does_not_replay_unknown_call(env):
    from app.services import material_group_runner as runner
    groups,db,settings,source,scheme=setup_groups(env);client=FakeClient()
    old,_=runner.create_recognition_run(db,settings,scheme,request_id="interrupted-old")
    plan=old["files"][0]
    db[groups.ATTEMPTS].insert_one({"_id":"inflight","run_id":old["_id"],"scheme_id":scheme["_id"],"file_id":plan["file_id"],
        "source_sha256":plan["sha256"],"model":scheme["model"],"prompt_version":scheme["prompt_version"],
        "menu_version":scheme["menu"]["menu_version"],"status":"running","actual_call":True,"created_at":store.now()})
    store.mark_scheme_interrupted(db)
    new=run_recognition(db,settings,scheme,client,"fresh-normal")
    assert client.calls==0 and new["counters"]["errors"]==1


@pytest.mark.parametrize("error",[
    CoreAIError("core_ai_upstream","HTTP 504",504),
    CoreAIError("core_ai_http_error","HTTP 408",408),
    CoreAIError("gateway_error","connection reset after request",502),
])
def test_ambiguous_transport_failures_are_not_automatically_replayed(env,error):
    groups,db,settings,source,scheme=setup_groups(env)
    client=FakeClient(error)
    first=run_recognition(db,settings,scheme,client,"transport-first")
    assert db[groups.ATTEMPTS].find_one({"run_id":first["_id"]})["status"]=="unknown"
    run_recognition(db,settings,scheme,client,"transport-normal-repeat")
    assert client.calls==1


def test_real_core_ai_http_504_mapping_does_not_replay(env):
    import httpx
    from app.services.core_ai import CoreAIClient
    groups,db,settings,source,scheme=setup_groups(env)
    calls=[]
    def gateway(request):
        calls.append(request.url.path)
        return httpx.Response(504,json={"message":"upstream timed out"})
    client=CoreAIClient(settings)
    client._client.close()
    client._client=httpx.Client(transport=httpx.MockTransport(gateway))
    try:
        first=run_recognition(db,settings,scheme,client,"real-mapping-first")
        assert db[groups.ATTEMPTS].find_one({"run_id":first["_id"]})["status"]=="unknown"
        run_recognition(db,settings,scheme,client,"real-mapping-repeat")
        assert len(calls)==1
    finally:client.close()
