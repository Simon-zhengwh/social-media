import json
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.routes import editable_projects as routes
from app.middleware import merchant_context_middleware
from app.services import editable_projects as service, maintenance
from tests.test_editable_projects import fixture


def web_client(fixture):
    db, settings, store, doc, project = fixture
    settings.am_bypass_local = True
    settings.social_media_origin = 'http://127.0.0.1:5173'
    maintenance.initialize(db)
    web = FastAPI()
    web.state.raw_db = db; web.state.db = db; web.state.store = store; web.state.local_store = store
    web.state.settings = settings
    web.middleware('http')(merchant_context_middleware)
    router = APIRouter(prefix='/api/merchants/{merchant_id}')
    router.include_router(routes.router); web.include_router(router)
    return TestClient(web), db, settings, store, doc, project


def test_task_project_routes_require_scope_write_header_and_revision(fixture):
    client, db, settings, store, doc, project = web_client(fixture)
    service.install(db, settings, store, doc, project, editor_html=b'<html></html>', report={'status': 'PASS'})
    base = f"/api/merchants/m1/whole-image/editable-projects/{doc['_id']}"
    current = client.get(base).json()
    assert current['project']['schema'] == 'image-editor-v2'
    assert client.get(base.replace('/m1/', '/m2/')).status_code == 404
    project['layers'][1]['text']['value'] = '$79.99'
    body = {'expected_revision': current['revision'], 'project': project}
    assert client.put(base, json=body).status_code == 403
    headers = {'Origin': 'http://127.0.0.1:5173', 'X-Media-Request': '1'}
    assert client.put(base, json=body, headers=headers).status_code == 200
    assert client.put(base, json=body, headers=headers).status_code == 409
    assert client.get(base).json()['project']['layers'][1]['text']['value'] == '$79.99'
    preview = client.get(base + '/preview')
    assert preview.status_code == 200
    assert 'font-src data:' in preview.headers['content-security-policy']
    assert "connect-src 'none'" in preview.headers['content-security-policy']
    assert 'allow-same-origin' not in preview.headers['content-security-policy']


def test_missing_v2_agent_is_explicit_and_does_not_create_or_trigger(fixture):
    client, db, _, _, _, _ = web_client(fixture)
    response = client.post('/api/merchants/m1/whole-image/tasks/task1/editable-projects',
        json={'source_version_id':'v1','source_output_id':'o1','request_id':'new'},
        headers={'Origin':'http://127.0.0.1:5173','X-Media-Request':'1'})
    assert response.status_code == 503
    assert db.whole_editable_projects.count_documents({}) == 1


def test_failed_existing_result_import_remains_queryable(fixture):
    client, db, _, _, doc, _ = web_client(fixture)
    calls = []
    client.app.state.core_ai = SimpleNamespace(poll_agent_detail=lambda run_id, **kw: calls.append(run_id) or SimpleNamespace(artifacts=[]))
    db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'status': 'failed', 'run_id': 'existing'}})
    url = f"/api/merchants/m1/whole-image/editable-projects/{doc['_id']}/refresh"
    headers = {'Origin': 'http://127.0.0.1:5173', 'X-Media-Request': '1'}
    assert client.post(url, headers=headers).status_code == 422
    assert db.whole_editable_projects.find_one({'_id': doc['_id']})['status'] == 'failed'
    assert client.post(url, headers=headers).status_code == 422
    assert calls == ['existing', 'existing']
