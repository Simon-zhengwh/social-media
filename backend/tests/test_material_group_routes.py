from __future__ import annotations

import time

from fastapi.testclient import TestClient

from tests.test_material_filter_routes import _build, _create_folder, _point_folder_at, WRITE_HEADERS
from tests.test_material_groups import jpeg


def wait(client,path):
    for _ in range(100):
        response=client.get(path)
        assert response.status_code==200,response.text
        result=response.json()
        if result["status"] in {"succeeded","failed","interrupted"}:return result
        time.sleep(.02)
    raise AssertionError("background task did not complete")


def test_local_group_api_lists_unrecognized_members_and_guards_selected_operations(monkeypatch,tmp_path):
    app,raw,settings=_build(monkeypatch,tmp_path)
    source=tmp_path/"images";source.mkdir()
    for name in ["a.jpg","b.jpg"]:(source/name).write_bytes(jpeg())
    with TestClient(app) as client:
        folder=_create_folder(client)
        _point_folder_at(raw,folder["id"],source)
        menu={"merchant_id":"m1","menu_version":"v1","name_format":"en","items":[{"id":"dish","zh":None,"en":"Test Dish"}]}
        from app.services import material_filters as store
        from app.services.scoped_db import ScopedDatabase
        db=ScopedDatabase(raw,"m1")
        scheme=store.create_scheme(db,settings,store.get_folder(db,folder["id"]),name="menus",naming_mode="menu_match",menu=menu)
        base="/api/merchants/m1/material-filters"
        started=client.post(f"{base}/schemes/{scheme['_id']}/group-scans",json={"request_id":"scan-first"},headers=WRITE_HEADERS)
        assert started.status_code==202,started.text
        done=wait(client,base+"/group-runs/"+started.json()["id"])
        assert done["status"]=="succeeded"
        assert app.state.core_ai.calls==0
        page=client.get(f"{base}/schemes/{scheme['_id']}/groups").json()
        assert page["stats"]["files"]==2 and page["stats"]["new_calls"]==1
        group=page["items"][0]
        members=client.get(base+"/groups/"+group["id"]+"/files").json()
        assert members["total"]==2 and members["items"][0]["evaluation"] is None
        assert client.get(members["items"][0]["preview_url"]).status_code==200
        history=client.get(base+"/files/"+members["items"][0]["id"]+"/attempts")
        assert history.status_code==200 and history.json()["total"]==0
        assert client.get("/api/merchants/other/material-filters/files/"+members["items"][0]["id"]+"/attempts").status_code in {403,404}
        assert client.get("/api/merchants/other/material-filters/groups/"+group["id"]+"/files").status_code in {403,404}
        bad=client.post(f"{base}/schemes/{scheme['_id']}/recognition-runs",json={"request_id":"force-bad","mode":"representatives","force":True},headers=WRITE_HEADERS)
        assert bad.status_code==422
        blocked=client.post(f"{base}/schemes/{scheme['_id']}/runs",json={"request_id":"legacy-all"},headers=WRITE_HEADERS)
        assert blocked.status_code==409
        split=client.post(base+"/groups/"+group["id"]+"/split",json={"file_ids":[members["items"][0]["id"]],"version":group["version"]},headers=WRITE_HEADERS)
        assert split.status_code==200,split.text
        assert [g["count"] for g in split.json()["groups"]]==[1,1]
        assert app.state.core_ai.calls==0
        parts=split.json()["groups"]
        payload={"group_ids":[g["id"] for g in parts],"version":parts[0]["version"],"request_id":"manual-merge"}
        merged=client.post(f"{base}/schemes/{scheme['_id']}/groups/merge",json=payload,headers=WRITE_HEADERS)
        assert merged.status_code==200,merged.text
        assert merged.json()["group"]["manual"] and merged.json()["group"]["count"]==2
        previews=merged.json()["group"]["previews"]
        assert len(previews)==2 and client.get(previews[0]["preview_url"]).status_code==200
        assert "features" not in previews[0] and "evaluation" not in previews[0]
        replay=client.post(f"{base}/schemes/{scheme['_id']}/groups/merge",json=payload,headers=WRITE_HEADERS)
        assert replay.json()["group"]["id"]==merged.json()["group"]["id"]
        assert app.state.core_ai.calls==0
        # Older releases could stop between run insertion and kind assignment.
        legacy,_=store.create_run(db,scheme,request_id="group-merge:old-crashed")
        db[store.RUN_COLLECTION].update_one({"_id":legacy["_id"]},{"$set":{"status":"interrupted"}})
        store.release_scheme_run(db,scheme["_id"],legacy["_id"])
        for action in ["resume","retry-failed"]:
            response=client.post(f"{base}/runs/{legacy['_id']}/{action}",headers=WRITE_HEADERS)
            assert response.status_code==409,response.text
        assert app.state.core_ai.calls==0


def test_filter_group_routes_preserve_per_image_filtering(monkeypatch,tmp_path):
    from app.services import material_filters as store
    from app.services.scoped_db import ScopedDatabase
    app,raw,settings=_build(monkeypatch,tmp_path)
    source=tmp_path/"filter-images";source.mkdir();(source/"food.jpg").write_bytes(jpeg())
    with TestClient(app) as client:
        folder=_create_folder(client);_point_folder_at(raw,folder["id"],source)
        db=ScopedDatabase(raw,"m1")
        scheme=store.create_scheme(db,settings,store.get_folder(db,folder["id"]),name="旧过滤方案",rules=[{"id":"empty_seating_scene","text":"不能空座位","interpretation":"空场景不通过"}])
        base="/api/merchants/m1/material-filters"
        started=client.post(f"{base}/schemes/{scheme['_id']}/group-scans",json={"request_id":"filter-local-group"},headers=WRITE_HEADERS)
        assert started.status_code==202,started.text
        assert wait(client,base+"/group-runs/"+started.json()["id"])["counters"]["model_calls"]==0
        page=client.get(f"{base}/schemes/{scheme['_id']}/groups").json()
        assert page["sampling_enabled"] is False and page["stats"]["new_calls"]==0
        denied=client.post(f"{base}/schemes/{scheme['_id']}/recognition-runs",json={"request_id":"filter-sampling-forbidden"},headers=WRITE_HEADERS)
        assert denied.status_code==409 and app.state.core_ai.calls==0


def test_non_local_group_endpoint_is_rejected(monkeypatch,tmp_path):
    from types import SimpleNamespace
    app,_,_=_build(monkeypatch,tmp_path,local=False)
    app.state.am=SimpleNamespace(access=lambda cookie,merchant:{"merchant_id":merchant})
    with TestClient(app) as client:
        response=client.get("/api/merchants/m1/material-filters/schemes/unknown/groups")
        assert response.status_code in {401,403}


def test_group_name_patch_names_the_group_and_rejects_stale_versions(monkeypatch,tmp_path):
    app,raw,settings=_build(monkeypatch,tmp_path)
    source=tmp_path/"images";source.mkdir()
    for name in ["a.jpg","b.jpg"]:(source/name).write_bytes(jpeg())
    with TestClient(app) as client:
        folder=_create_folder(client)
        _point_folder_at(raw,folder["id"],source)
        menu={"merchant_id":"m1","menu_version":"v1","name_format":"en","items":[{"id":"dish","zh":None,"en":"Test Dish"}]}
        from app.services import material_filters as store
        from app.services.scoped_db import ScopedDatabase
        db=ScopedDatabase(raw,"m1")
        scheme=store.create_scheme(db,settings,store.get_folder(db,folder["id"]),name="menus",naming_mode="menu_match",menu=menu)
        base="/api/merchants/m1/material-filters"
        started=client.post(f"{base}/schemes/{scheme['_id']}/group-scans",json={"request_id":"scan-name"},headers=WRITE_HEADERS)
        assert started.status_code==202,started.text
        assert wait(client,base+"/group-runs/"+started.json()["id"])["status"]=="succeeded"
        group=client.get(f"{base}/schemes/{scheme['_id']}/groups").json()["items"][0]
        renamed=client.patch(base+"/groups/"+group["id"]+"/name",json={"name":"卤牛腱肉_Spiced Beef","version":group["version"]},headers=WRITE_HEADERS)
        assert renamed.status_code==200,renamed.text
        body=renamed.json()
        assert body["reference_name"]=="卤牛腱肉_Spiced Beef" and body["name_manual"] is True and body["status"]=="manual"
        assert client.patch(base+"/groups/"+group["id"]+"/name",json={"name":"另一个","version":"stale"},headers=WRITE_HEADERS).status_code==409
        assert client.patch(base+"/groups/"+group["id"]+"/name",json={"name":"","version":group["version"]},headers=WRITE_HEADERS).status_code==422
        foreign=base.replace("/m1/","/other/")
        assert client.patch(foreign+"/groups/"+group["id"]+"/name",json={"name":"x","version":group["version"]},headers=WRITE_HEADERS).status_code in {403,404}
        assert app.state.core_ai.calls==0
