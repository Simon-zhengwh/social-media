from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import threading
import uuid

import httpx
import mongomock
import pytest

from app.config import Settings
from app.services import agent_tests
from app.services.core_ai import CoreAIClient, CoreAIError
from app.services.scoped_db import ScopedDatabase
from tests.conftest import png_bytes


def test_benchmark_wait_defaults_to_15_minutes():
    assert Settings.model_fields['core_ai_test_timeout_seconds'].default == 1800


@pytest.fixture
def env(settings):
    root = settings.storage_path / 'agent-test-cases'
    root.mkdir(parents=True)
    data = png_bytes()
    (root/'poster.png').write_bytes(data)
    case = {'id': 'text', 'label': '文字修改', 'prompt': '只修改指定文字\r\n',
            'images': [{'file': 'poster.png', 'name': '原图.png', 'sha256': hashlib.sha256(data).hexdigest(), 'size': [320, 240]}]}
    (root/'manifest.json').write_text(json.dumps({'version': 'test-v1', 'cases': [case]}))
    return SimpleNamespace(settings=settings, db=mongomock.MongoClient().db, root=root, case=case)


class FakeClient:
    def __init__(self):
        self.uploads = []
        self.triggers = []
        self.reads = []
        self.last_input = ''

    def upload_image_blob(self, data, *, content_type):
        self.uploads.append(data)
        return 'https://example.com/fixed-original.png'

    def trigger_agent(self, agent_id, text, *, timeout_seconds, attachments=None):
        self.triggers.append((agent_id, text, attachments))
        self.last_input = text
        return 'existing-run'

    def get_agent_run(self, run_id):
        self.reads.append(run_id)
        return {'id': run_id, 'status': 'COMPLETED', 'trace_id': 'actual-trace', 'input': self.last_input, 'output': '完成',
                'transcript': [{'ts': '2026-11-04T00:00:00Z', 'role': 'tool', 'name': 'use_skill', 'result': 'loaded'}],
                'artifacts': [{'file_name': 'editor.html', 'content_type': 'text/html', 'download_url': 'https://example.com/editor.html'}]}


def test_fixed_inputs_raw_prompt_cached_uploads_and_idempotency(env):
    client = FakeClient()
    key = str(uuid.uuid4())
    batch = agent_tests.create_batch(env.db, env.settings, key, ['text'], '优化版')
    again = agent_tests.create_batch(env.db, env.settings, key, ['text'], '优化版')
    assert batch['id'] == again['id']
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    assert len(client.triggers) == 1
    expected_input = env.case['prompt'].rstrip() + '\n\nhttps://example.com/fixed-original.png'
    assert client.triggers[0][1] == expected_input
    assert client.triggers[0][2] is None
    result = agent_tests.get_batch(env.db, batch['id'])
    assert result['runs'][0]['user_input'] == env.case['prompt']
    assert result['runs'][0]['submitted_input'] == expected_input
    assert result['runs'][0]['remote_input'] == expected_input
    assert result['runs'][0]['trace_id'] == 'actual-trace'
    assert result['runs'][0]['artifacts'][0]['file_name'] == 'editor.html'
    second = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '')
    agent_tests.run_batch(env.db, env.settings, client, second['id'])
    assert len(client.uploads) == 1
    assert len(client.triggers) == 2


def test_batch_cases_trigger_in_parallel(env):
    barrier = threading.Barrier(2)

    class ParallelClient(FakeClient):
        def trigger_agent(self, agent_id, text, *, timeout_seconds, attachments=None):
            self.triggers.append((agent_id, text, attachments))
            barrier.wait(timeout=3)
            return 'parallel-run'

    client = ParallelClient()
    manifest = json.loads((env.root/'manifest.json').read_text())
    second_case = json.loads(json.dumps(env.case))
    second_case['id'] = 'text2'
    manifest['cases'].append(second_case)
    (env.root/'manifest.json').write_text(json.dumps(manifest))
    batch = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text', 'text2'], 'parallel')
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    assert len(client.triggers) == 2
    assert [run['status'] for run in agent_tests.get_batch(env.db, batch['id'])['runs']] == ['completed', 'completed']


def test_wait_timeout_marks_failed_without_polling_after_30_minutes(env, monkeypatch):
    class RunningClient(FakeClient):
        def get_agent_run(self, run_id):
            self.reads.append(run_id)
            return {'id': run_id, 'status': 'RUNNING', 'transcript': []}

    clock = [0.0]
    monkeypatch.setattr(agent_tests.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(agent_tests.time, 'sleep', lambda _: clock.__setitem__(0, 1801.0))
    client = RunningClient()
    batch = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '')
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    run = agent_tests.get_batch(env.db, batch['id'])['runs'][0]
    assert run['status'] == 'failed'
    assert '30分钟' in run['error']
    assert client.reads == ['existing-run']


def test_unknown_trigger_never_reposts_on_resume(env):
    class UnknownClient(FakeClient):
        def trigger_agent(self, *args, **kwargs):
            self.triggers.append(args)
            raise CoreAIError('agent_timeout', '结果未知', 504)
    client = UnknownClient()
    batch = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '')
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    assert len(client.triggers) == 1
    assert agent_tests.get_batch(env.db, batch['id'])['runs'][0]['status'] == 'trigger_unknown'


def test_changed_baseline_is_blocked_before_remote_calls(env):
    (env.root/'poster.png').write_bytes(png_bytes(color=(0, 0, 0)))
    with pytest.raises(CoreAIError, match='基准图片'):
        agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '')


def test_api_client_preserves_attachments_and_reads_trace_artifacts(settings):
    seen = []
    def handle(request):
        seen.append(request)
        if request.method == 'POST':
            return httpx.Response(202, json={'run_id': 'run-1'})
        return httpx.Response(200, json={'id': 'run-1', 'trace_id': 'trace-1', 'status': 'RUNNING', 'transcript': []})
    client = CoreAIClient(settings, transport=httpx.MockTransport(handle))
    try:
        attachments = [{'type': 'IMAGE', 'url': 'https://example.com/original.png', 'media_type': 'image/png'}]
        assert client.trigger_agent('agent-1', '原文\r\n', timeout_seconds=3600, attachments=attachments) == 'run-1'
        assert json.loads(seen[0].content)['input'] == '原文\r\n'
        assert json.loads(seen[0].content)['attachments'] == attachments
        assert client.get_agent_run('run-1')['trace_id'] == 'trace-1'
        assert seen[-1].url.path == '/api/runs/run-1'
    finally:
        client.close()


def test_resume_reads_same_run_and_details_redact_local_key(env):
    client = FakeClient()
    batch = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '')
    local_id = batch['runs'][0]['id']
    env.db.agent_test_runs.update_one({'_id': local_id}, {'$set': {'status': 'wait_timeout', 'run_id': 'already-started'}})
    agent_tests.run_batch(env.db, env.settings, client, batch['id'])
    assert client.triggers == [] and client.reads == ['already-started']
    agent_tests._save_detail(env.db, env.settings, local_id,
                            {'status': 'RUNNING', 'output': env.settings.core_ai_api_key})
    assert env.settings.core_ai_api_key not in agent_tests.report_path(env.settings, local_id).read_text()


def test_raw_report_route_and_case_images_are_available(env):
    env.db = ScopedDatabase(env.db, 'merchant')
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.agent_tests import router
    web = FastAPI()
    web.include_router(router, prefix='/api/merchants/merchant')
    web.state.settings, web.state.db, web.state.core_ai = env.settings, env.db, FakeClient()
    with TestClient(web) as client:
        cases = client.get('/api/merchants/merchant/agent-tests/cases').json()
        assert cases['cases'][0]['prompt'] == env.case['prompt']
        assert client.get('/api/merchants/merchant/agent-tests/cases/text/images/0').content == png_bytes()
        response = client.post('/api/merchants/merchant/agent-tests/batches', json={'request_id': str(uuid.uuid4()), 'case_ids': ['text']})
        assert response.status_code == 202
        batch_id = response.json()['id']
        done = client.get('/api/merchants/merchant/agent-tests/batches/'+batch_id).json()
        assert done['runs'][0]['trace_url'].endswith('/traces/actual-trace')
        assert client.get(done['runs'][0]['report_url']).json()['trace_id'] == 'actual-trace'


def test_history_returns_all_batches_in_newest_first_order(env):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 11, 4, tzinfo=timezone.utc)
    env.db.agent_test_batches.insert_many([
        {'_id': str(index), 'created_at': (start + timedelta(minutes=index)).isoformat(),
         'note': f'batch {index}', 'case_ids': ['text']}
        for index in range(35)
    ])
    history = agent_tests.list_batches(env.db)
    assert [item['id'] for item in history] == [str(index) for index in reversed(range(35))]
    assert all(item['hidden'] is False for item in history)


def test_batch_can_be_hidden_found_and_restored_without_changing_runs(env):
    env.db = ScopedDatabase(env.db, 'merchant')
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.agent_tests import router

    remote = FakeClient()
    batch = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '不理想的结果')
    agent_tests.run_batch(env.db, env.settings, remote, batch['id'])
    completed = agent_tests.get_batch(env.db, batch['id'])
    original_runs = list(env.db.agent_test_runs.find())
    web = FastAPI()
    web.include_router(router, prefix='/api/merchants/merchant')
    web.state.db = env.db
    url = '/api/merchants/merchant/agent-tests/batches'
    with TestClient(web) as client:
        for _ in range(2):
            response = client.patch(f'{url}/{batch["id"]}/visibility', json={'hidden': True})
            assert response.status_code == 200
            assert response.json()['hidden'] is True
        assert client.get(url).json() == []
        hidden = client.get(url+'?include_hidden=true').json()
        assert len(hidden) == 1 and hidden[0]['id'] == batch['id'] and hidden[0]['hidden'] is True
        # Hidden history remains directly accessible, including its original artifacts.
        assert client.get(f'{url}/{batch["id"]}').json()['runs'] == completed['runs']
        assert client.get(f'{url}/{batch["id"]}').json()['runs'][0]['artifacts'][0]['file_name'] == 'editor.html'
        response = client.patch(f'{url}/{batch["id"]}/visibility', json={'hidden': False})
        assert response.status_code == 200 and response.json()['hidden'] is False
        assert client.get(url).json()[0]['id'] == batch['id']
    assert list(env.db.agent_test_runs.find()) == original_runs
    assert len(remote.triggers) == 1


def test_visibility_rejects_invalid_or_missing_batches_without_creating_records(env):
    env.db = ScopedDatabase(env.db, 'merchant')
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.agent_tests import router

    web = FastAPI()
    web.include_router(router, prefix='/api/merchants/merchant')
    web.state.db = env.db
    url = '/api/merchants/merchant/agent-tests/batches/missing/visibility'
    with TestClient(web) as client:
        assert client.patch(url, json={'hidden': True}).status_code == 404
        assert client.patch(url, json={}).status_code == 422
        assert client.patch(url, json={'hidden': 'false'}).status_code == 422
    assert env.db.agent_test_batches.count_documents({}) == 0


def test_html_preview_is_inline_isolated_and_cached(env):
    env.db = ScopedDatabase(env.db, 'merchant')
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes.agent_tests import router
    web = FastAPI()
    web.include_router(router, prefix='/api/merchants/merchant')
    class HtmlClient(FakeClient):
        html_reads = 0
        def read_agent_html(self, url):
            self.html_reads += 1
            return b'<!doctype html><script>document.title="fixture"</script><h1>Editor fixture</h1>'
    remote = HtmlClient()
    web.state.settings, web.state.db, web.state.core_ai = env.settings, env.db, remote
    batch = agent_tests.create_batch(env.db, env.settings, str(uuid.uuid4()), ['text'], '')
    agent_tests.run_batch(env.db, env.settings, remote, batch['id'])
    local_id = batch['runs'][0]['id']
    with TestClient(web) as client:
        url = '/api/merchants/merchant/agent-tests/runs/'+local_id+'/artifacts/0/preview'
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['content-disposition'] == 'inline'
        csp = response.headers['content-security-policy']
        assert 'sandbox allow-scripts allow-downloads;' in csp and 'allow-same-origin' not in csp
        assert "connect-src 'none'" in csp
        assert client.get(url).content == response.content
        assert remote.html_reads == 1


def test_html_download_follows_redirect_without_auth_header(settings, monkeypatch):
    requests = []
    def handle(request):
        requests.append(request)
        if request.url.host == 'core-ai-server.connexup-uat.net':
            return httpx.Response(307, headers={'Location': 'https://cdn.example.com/actual.html'})
        return httpx.Response(200, content=b'<html>actual artifact</html>')
    client = CoreAIClient(settings, transport=httpx.MockTransport(handle))
    checked = []
    monkeypatch.setattr(client, '_validate_public_url', checked.append)
    try:
        assert client.read_agent_html(settings.core_ai_server_base_url+'/api/public/artifacts/actual/content') == b'<html>actual artifact</html>'
        assert len(checked) == 2
        assert all('authorization' not in r.headers for r in requests)
        with pytest.raises(CoreAIError, match='当前平台'):
            client.read_agent_html('https://unrelated.example.com/page.html')
    finally:
        client.close()


def test_edit_agent_defaults_match_the_benchmark_agent():
    assert (
        Settings.model_fields["core_ai_image_agent_id"].default
        == "5a162c34-f333-453f-9835-de6a4f96e3fb"
    )
    assert (
        Settings.model_fields["core_ai_image_agent_timeout_seconds"].default == 1800
    )


def test_same_request_uuid_is_independent_for_each_merchant(env):
    raw = env.db
    first, second = ScopedDatabase(raw, 'merchant-a'), ScopedDatabase(raw, 'merchant-b')
    request_id = str(uuid.uuid4())
    a = agent_tests.create_batch(first, env.settings, request_id, ['text'], 'a')
    b = agent_tests.create_batch(second, env.settings, request_id, ['text'], 'b')
    assert a['id'] == b['id'] == request_id
    assert a['runs'][0]['id'] != b['runs'][0]['id']
    agent_tests.run_batch(first, env.settings, FakeClient(), request_id)
    assert agent_tests.get_batch(first, request_id)['runs'][0]['status'] == 'completed'
    assert agent_tests.get_batch(second, request_id)['runs'][0]['status'] == 'queued'
    assert agent_tests.list_batches(first)[0]['id'] == request_id
    assert agent_tests.recover(second) == [request_id]


def test_benchmark_media_upload_cache_is_merchant_scoped(env):
    first, second = ScopedDatabase(env.db, 'merchant-a'), ScopedDatabase(env.db, 'merchant-b')
    client_a, client_b = FakeClient(), FakeClient()
    agent_tests._media_urls(first, env.settings, client_a, env.case)
    agent_tests._media_urls(first, env.settings, client_a, env.case)
    agent_tests._media_urls(second, env.settings, client_b, env.case)
    assert len(client_a.uploads) == len(client_b.uploads) == 1
    assert env.db.agent_test_media.count_documents({}) == 2


def scoped_request(env, merchant_id, remote):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(settings=env.settings, db=env.db, core_ai=remote)),
        state=SimpleNamespace(db=ScopedDatabase(env.db, merchant_id), merchant_id=merchant_id),
    )


def test_report_does_not_serve_another_merchants_cached_file(env):
    from fastapi import HTTPException
    from app.routes import agent_tests as routes

    run_id = 'a' * 32
    env.db.agent_test_runs.insert_one({'_id': run_id, 'merchant_id': 'merchant-a', 'run_id': 'already-started'})
    agent_tests._save_detail(ScopedDatabase(env.db, 'merchant-a'), env.settings, run_id, {'status': 'COMPLETED', 'output': 'private'})
    with pytest.raises(HTTPException) as rejected:
        routes.report(run_id, scoped_request(env, 'merchant-b', FakeClient()))
    assert rejected.value.status_code == 404


def test_missing_report_cache_reads_the_saved_remote_run_without_triggering(env):
    from app.routes import agent_tests as routes

    run_id = 'b' * 32
    env.db.agent_test_runs.insert_one({'_id': run_id, 'merchant_id': 'merchant-a', 'run_id': 'already-started'})
    remote = FakeClient()
    response = routes.report(run_id, scoped_request(env, 'merchant-a', remote))
    assert json.loads(Path(response.path).read_text(encoding='utf-8'))['trace_id'] == 'actual-trace'
    assert remote.reads == ['already-started'] and remote.triggers == []
    assert env.db.agent_test_runs.find_one({'_id': run_id})['report_url'] == f'/api/merchants/merchant-a/agent-tests/runs/{run_id}/report'
    cached = routes.report(run_id, scoped_request(env, 'merchant-a', remote))
    assert cached.path == response.path
    assert remote.reads == ['already-started']


def test_fixed_case_image_urls_use_the_authenticated_merchant_path(env):
    from app.routes import agent_tests as routes

    catalog = routes.cases(scoped_request(env, 'merchant/a', FakeClient()))
    image = catalog['cases'][0]['images'][0]
    assert image['preview_url'] == '/api/merchants/merchant%2Fa/agent-tests/cases/text/images/0?preview=true'
    assert image['original_url'] == '/api/merchants/merchant%2Fa/agent-tests/cases/text/images/0'
