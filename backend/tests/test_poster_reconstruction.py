from datetime import timedelta
from copy import deepcopy
import hashlib
import io
import json
from types import SimpleNamespace
from threading import Barrier
import zipfile
import httpx

import pytest
from PIL import Image

from app.services import poster_reconstruction as runner
from app.services import editable_projects as projects
from app.services.core_ai import CoreAIClient
from tests.test_editable_projects import fixture, image_url


def bundle(project, poster_sha, *, visual='PASS'):
    raw = json.dumps(project, ensure_ascii=False).encode()
    png = io.BytesIO(); Image.new('RGB', (64, 48), 'green').save(png, format='PNG')
    final = png.getvalue()
    report = {'status': 'PASS', 'scopes': {'technical': 'PASS', 'browser': 'PASS', 'text': 'PASS', 'visual': visual},
              'project_sha256': hashlib.sha256(raw).hexdigest(), 'poster_sha256': poster_sha,
              'final_sha256': hashlib.sha256(final).hexdigest()}
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as z:
        for name, data in [('project.json', raw), ('editor.html', b'<html>editor</html>'), ('final.png', final), ('qa-report.json', json.dumps(report).encode())]:
            z.writestr(name, data)
    return output.getvalue()


def test_bundle_requires_current_file_hashes_and_real_visual_result(fixture):
    _, _, _, _, project = fixture
    digest = 'a' * 64
    assert runner.unpack_bundle(bundle(project, digest), expected_poster_sha=digest, expected_size=(64, 48))[0] == project
    with pytest.raises(projects.ProjectError):
        runner.unpack_bundle(bundle(project, digest, visual='not_performed'), expected_poster_sha=digest, expected_size=(64, 48))
    with pytest.raises(projects.ProjectError):
        runner.unpack_bundle(bundle(project, digest), expected_poster_sha='b' * 64, expected_size=(64, 48))


def test_expired_run_never_triggers_new_generation(fixture):
    db, settings, store, doc, _ = fixture
    db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'deadline_at': projects.now() - timedelta(seconds=1), 'run_id': 'existing'}})
    calls = []
    client = SimpleNamespace(trigger_agent=lambda *a, **kw: calls.append('trigger'), poll_agent_detail=lambda *a: None)
    runner.run(db, settings, store, client, doc['_id'])
    state = db.whole_editable_projects.find_one({'_id': doc['_id']})
    assert state['status'] == 'failed' and state['run_id'] == 'existing'
    assert not calls


def test_saved_run_is_polled_instead_of_retriggered(fixture, monkeypatch):
    db, settings, store, doc, project = fixture
    db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'run_id': 'existing'}})
    calls = []
    result = SimpleNamespace(status='completed', artifacts=[])
    client = SimpleNamespace(trigger_agent=lambda *a, **kw: calls.append('trigger'), poll_agent_detail=lambda rid, **kw: calls.append(rid) or result)
    monkeypatch.setattr(runner, 'install_result', lambda *a, **kw: db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'status': 'ready'}}))
    runner.run(db, settings, store, client, doc['_id'])
    assert calls == ['existing']
    assert db.whole_editable_projects.find_one({'_id': doc['_id']})['status'] == 'ready'


def test_real_client_can_include_verified_bundle_files_without_changing_image_poll(settings):
    payload = {'status': 'COMPLETED', 'output': 'done', 'artifacts': [
        {'file_name': 'result.zip', 'content_type': 'application/zip', 'download_url': 'https://example.com/result.zip'},
        {'file_name': 'final.png', 'content_type': 'image/png', 'download_url': 'https://example.com/final.png'}]}
    client = CoreAIClient(settings, transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
    try:
        assert len(client.poll_agent_detail('run1').artifacts) == 1
        assert [a.file_name for a in client.poll_agent_detail('run1', include_files=True).artifacts] == ['result.zip', 'final.png']
    finally:
        client.close()


def test_resume_queries_multiple_known_runs_independently(fixture, monkeypatch):
    db, settings, store, doc, _ = fixture
    second = deepcopy(doc); second['_id'] = 'project2'; second['request_id'] = 'second'; second['run_id'] = 'run2'
    db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'run_id': 'run1'}})
    db.whole_editable_projects.insert_one(second)
    barrier = Barrier(2)
    visited = []
    def independent(*args):
        visited.append(args[-1]); barrier.wait(timeout=3)
    monkeypatch.setattr(runner, 'run', independent)
    runner.resume(db, settings, store, SimpleNamespace())
    assert set(visited) == {doc['_id'], 'project2'}


def test_preparation_deadline_does_not_start_late_run(fixture, monkeypatch):
    db, settings, store, doc, _ = fixture
    current = projects.now()
    db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'deadline_at': current + timedelta(seconds=10)}})
    monkeypatch.setattr(projects, 'now', lambda: current)
    def preparation(*args):
        nonlocal current
        current += timedelta(seconds=11)
        return 'prepared'
    monkeypatch.setattr(runner, '_input', preparation)
    calls = []
    settings.core_ai_editable_poster_agent_id = 'v2-agent'
    runner.run(db, settings, store, SimpleNamespace(trigger_agent=lambda *a, **kw: calls.append('trigger')), doc['_id'])
    assert calls == []
    assert db.whole_editable_projects.find_one({'_id': doc['_id']})['status'] == 'failed'


def test_unverified_html_does_not_become_a_ready_project(fixture):
    _, _, _, _, project = fixture
    from app.services.editable_project_verification import verify
    with pytest.raises(projects.ProjectError):
        verify(project, b'<html>editor</html>', b'not a png')


def test_each_actual_photo_requires_a_matching_used_source_mapping(fixture, monkeypatch):
    db, settings, store, doc, project = fixture
    raw = projects._decode(image_url())[1]
    for asset_id in ('poster1', 'photo1'):
        store.put_bytes(asset_id + '.png', raw, 'image/png')
        db.assets.insert_one({'_id': asset_id, 'path': asset_id + '.png'})
    project['assets']['photo-asset'] = image_url()
    project['provenance']['photo-asset'] = {'role':'photo','source':'original','sha256':hashlib.sha256(raw).hexdigest()}
    photo = deepcopy(project['layers'][0]); photo.update(id='photo',type='photo',assetId='photo-asset')
    project['layers'].append(photo)
    report = {'status':'PASS','source_mapping':[{'input_asset_id':'photo1','used':False,'layer_ids':[],'reason':'not used'}]}
    monkeypatch.setattr(runner, 'unpack_bundle', lambda *a, **kw: (project,b'<html>fixture</html>',report))
    client = SimpleNamespace(download_agent_file=lambda *a, **kw: b'fixture')
    result = SimpleNamespace(artifacts=[SimpleNamespace(file_name='result.zip',content_type='application/zip',download_url='https://example.com/result.zip')])
    with pytest.raises(projects.ProjectError, match='实际原照片图层'):
        runner.install_result(db, settings, store, client, doc, result)
    assert db.whole_editable_projects.find_one({'_id':doc['_id']})['status'] == 'queued'
    report['source_mapping'][0].update(used=True,layer_ids=['photo'],reason='visible source photo')
    runner.install_result(db, settings, store, client, doc, result)
    assert db.whole_editable_projects.find_one({'_id':doc['_id']})['status'] == 'ready'
