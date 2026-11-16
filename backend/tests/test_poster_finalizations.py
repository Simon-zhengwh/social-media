from datetime import datetime, timezone
import hashlib

import mongomock
import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from app.middleware import merchant_context_middleware
from app.routes import assets, merchant_context, whole_image
from app.services import maintenance
from app.services.poster_finalizations import enrich_assets
from app.services.scoped_db import ScopedDatabase


HEADERS = {'Origin': 'http://127.0.0.1:5173', 'X-Media-Request': '1'}
BASE = '/api/merchants/m1'


@pytest.fixture
def finalization_app(settings, store):
    db = mongomock.MongoClient().db
    settings.am_bypass_local = True
    settings.social_media_origin = 'http://127.0.0.1:5173'
    db.merchants.insert_many([{'_id': 'm1', 'name': 'One'}, {'_id': 'm2', 'name': 'Two'}])
    maintenance.initialize(db)
    web = FastAPI()
    web.state.raw_db = db
    web.state.db = db
    web.state.settings = settings
    web.state.local_store = store
    web.state.store = store
    web.middleware('http')(merchant_context_middleware)
    router = APIRouter(prefix='/api/merchants/{merchant_id}')
    for item in (whole_image.router, assets.router, merchant_context.router):
        router.include_router(item)
    web.include_router(router)
    now = datetime.now(timezone.utc)
    for merchant in ('m1', 'm2'):
        for asset in ('a', 'b', 'logo', 'output', 'sibling'):
            db.assets.insert_one({'_id': f'{merchant}-{asset}', 'merchant_id': merchant,
                'original_name': asset + '.png', 'width': 320, 'height': 240, 'mime_type': 'image/png',
                'source': 'generated' if asset == 'output' else 'upload', 'created_at': now})
    seed_task(db, 't1', 'workspace', 'm1', ['m1-a', 'm1-logo'])
    return TestClient(web), db


def seed_task(db, task_id, workspace, merchant, photos):
    now = datetime.now(timezone.utc)
    db.whole_tasks.insert_one({'_id': task_id, 'merchant_id': merchant, 'status': 'succeeded',
        'stage': 'completed', 'progress': 100, 'input': {'generation_workspace_id': workspace,
            'reference_asset_ids': photos, 'logo_asset_id': f'{merchant}-logo'},
        'current_version_id': task_id + '-v1', 'selected_version_id': task_id + '-v1',
        'current_output_id': 'result', 'current_version': 1, 'edit': {'status': 'idle'},
        'created_at': now, 'updated_at': now})
    seed_version(db, task_id, task_id + '-v1', merchant, photos)


def seed_version(db, task_id, version_id, merchant, photos, parent=None, replacements=None):
    db.whole_versions.insert_one({'_id': version_id, 'merchant_id': merchant, 'task_id': task_id,
        'version': 2 if parent else 1, 'parent_version_id': parent,
        'operation': 'original_replace' if replacements else ('edit' if parent else 'generate'),
        'replacements': replacements or [], 'width': 320, 'height': 240,
        'outputs': [{'id': 'result', 'asset_id': f'{merchant}-output', 'selectable': True}],
        'model_input': {'images': [{'asset_id': item,
            'role': 'logo' if item.endswith('logo') else 'reference'} for item in photos]},
        'created_at': datetime.now(timezone.utc)})


def endpoint(task='t1', base=BASE):
    return f'{base}/whole-image/tasks/{task}/finalization'


def save(client, task='t1', photos=None, version=None, revision=None, base=BASE):
    body = {
        'version_id': version or task + '-v1', 'output_id': 'result',
        'expected_revision': revision}
    if photos is not None:
        body['asset_ids'] = photos
    return client.put(endpoint(task, base), headers=HEADERS, json=body)


def count(client, asset='m1-a'):
    page = client.get(BASE + '/am-assets?limit=100').json()
    return next(item for item in page['items'] if item['id'] == asset)['usage_count']


def test_checklist_follows_selected_ancestry_and_excludes_logo_generated_and_sibling(finalization_app):
    client, db = finalization_app
    seed_version(db, 't1', 'v2', 'm1', ['m1-b', 'm1-output'], parent='t1-v1')
    seed_version(db, 't1', 'other-branch', 'm1', ['m1-sibling'], parent='t1-v1')
    response = client.get(endpoint() + '?version_id=v2&output_id=result')
    assert response.status_code == 200
    assert response.json()['finalization'] is None
    assert {item['id'] for item in response.json()['assets']} == {'m1-a', 'm1-b'}


def test_source_photos_count_automatically_and_identical_retry_preserves_time(finalization_app):
    client, db = finalization_app
    assert count(client) == 0
    saved = save(client)
    assert saved.status_code == 200
    final = saved.json()
    assert final['asset_ids'] == ['m1-a']
    assert final['finalized_at'].endswith('Z')
    assert count(client) == 1
    assert count(client, 'm1-logo') == 0
    assert save(client).json() == final
    assert db.whole_finalizations.count_documents({}) == 1


def test_replace_across_candidates_recalculates_and_revoke_removes_record(finalization_app):
    client, db = finalization_app
    original = save(client).json()
    seed_task(db, 't2', 'workspace', 'm1', ['m1-b'])
    saved = save(client, 't2', ['m1-b'], revision=original.get('revision'))
    assert saved.status_code == 200
    assert count(client) == 0
    assert count(client, 'm1-b') == 1
    assert db.whole_finalizations.count_documents({}) == 1
    assert client.get(endpoint()).json()['finalization']['task_id'] == 't2'
    final = saved.json()
    details = client.get(BASE + '/assets/m1-b/usages').json()
    assert len(details) == 1 and details[0]['version_id'] == 't2-v1'
    assert details[0]['finalized_at'] == final['finalized_at']
    assert details[0]['preview_url'] and 'whole=t2' in details[0]['task_url']
    response = client.delete(endpoint() + '?expected_revision=' + final['revision'], headers=HEADERS)
    assert response.status_code == 204
    assert count(client, 'm1-b') == 0
    assert client.get(BASE + '/assets/m1-b/usages').json() == []
    assert db.whole_finalizations.count_documents({}) == 0


def test_multiple_workspaces_count_separately_and_draft_edit_keeps_final(finalization_app):
    client, db = finalization_app
    saved = save(client).json()
    seed_task(db, 't2', 'different', 'm1', ['m1-a'])
    assert save(client, 't2').status_code == 200
    db.whole_tasks.update_one({'_id': 't1'}, {'$set': {'edit.status': 'running'}})
    assert count(client) == 2
    assert client.get(endpoint()).json()['finalization'] == saved
    assert len(client.get(BASE + '/assets/m1-a/usages').json()) == 2


@pytest.mark.parametrize('photos', [['m1-b'], ['m1-logo'], ['m1-output'], ['m2-a'], ['missing'], []])
def test_browser_cannot_override_recorded_source_photos(finalization_app, photos):
    client, db = finalization_app
    response = save(client, photos=photos)
    assert response.status_code == 200
    assert response.json()['asset_ids'] == ['m1-a']
    assert db.whole_finalizations.count_documents({}) == 1


def test_one_click_finalization_uses_the_two_recorded_inputs_without_a_photo_list(finalization_app):
    client, db = finalization_app
    db.whole_tasks.update_one({'_id': 't1'}, {'$set': {'input.reference_asset_ids': ['m1-a', 'm1-b', 'm1-logo']}})
    db.whole_versions.update_one({'_id': 't1-v1'}, {'$push': {
        'model_input.images': {'asset_id': 'm1-b', 'role': 'reference'}}})
    response = save(client)
    assert response.status_code == 200
    assert response.json()['asset_ids'] == ['m1-a', 'm1-b']
    assert count(client, 'm1-a') == count(client, 'm1-b') == 1
    assert count(client, 'm1-logo') == 0


def test_stale_dialog_cannot_overwrite_or_revoke_new_finalization(finalization_app):
    client, db = finalization_app
    first = save(client).json()
    seed_version(db, 't1', 'v2', 'm1', ['m1-b'], parent='t1-v1')
    assert save(client, photos=['m1-b'], version='v2').status_code == 409
    updated = save(client, photos=['m1-b'], version='v2', revision=first.get('revision'))
    assert updated.status_code == 200
    assert save(client, revision=first['revision']).status_code == 409
    assert client.delete(endpoint() + '?expected_revision=' + first['revision'], headers=HEADERS).status_code == 409
    assert client.get(endpoint()).json()['finalization'] == updated.json()


def test_scope_write_protection_and_invalid_output(finalization_app):
    client, db = finalization_app
    assert client.put(endpoint(), json={}).status_code == 403
    assert client.get(endpoint(base='/api/merchants/m2')).status_code == 404
    assert save(client, version='missing').status_code == 404
    body = {'version_id': 't1-v1', 'output_id': 'missing', 'asset_ids': []}
    assert client.put(endpoint(), headers=HEADERS, json=body).status_code == 422
    assert save(client, photos=[]).status_code == 200


def test_am_aliases_share_counts_and_detail_without_an_am_write(finalization_app):
    client, db = finalization_app
    db.assets.update_one({'_id': 'm1-a'}, {'$set': {'am_media_library_id': 'media-a'}})
    assert save(client).status_code == 200
    internal = client.get(BASE + '/assets/m1-a/usages')
    external = client.get(BASE + '/assets/am:media-a/usages')
    assert internal.status_code == external.status_code == 200
    assert len(external.json()) == 1 and external.json() == internal.json()


def test_finalized_candidate_must_be_revoked_before_deletion(finalization_app):
    client, db = finalization_app
    assert save(client).status_code == 200
    response = client.delete(BASE + '/whole-image/tasks/t1/candidate', headers=HEADERS)
    assert response.status_code == 409
    assert '定稿' in response.json()['detail']


def test_original_replacement_photos_and_legacy_batch_are_supported(finalization_app):
    client, db = finalization_app
    db.whole_tasks.update_one({'_id': 't1'}, {'$unset': {'input.generation_workspace_id': ''},
        '$set': {'input.candidate_batch_id': 'old-batch'}})
    seed_version(db, 't1', 'replacement', 'm1', [], parent='t1-v1', replacements=[{'asset_id': 'm1-b', 'target': '菜品'}])
    response = client.get(endpoint() + '?version_id=replacement&output_id=result')
    assert response.status_code == 200
    assert {item['id'] for item in response.json()['assets']} == {'m1-a', 'm1-b'}
    assert save(client, photos=['m1-b'], version='replacement').status_code == 200
    assert count(client, 'm1-b') == 1


def test_same_workspace_key_is_isolated_between_merchants(finalization_app):
    client, db = finalization_app
    first = save(client)
    assert first.status_code == 200
    seed_task(db, 'foreign', 'workspace', 'm2', ['m2-a'])
    second = save(client, task='foreign', photos=['m2-a'], base='/api/merchants/m2')
    assert second.status_code == 200
    assert first.json()['id'] != second.json()['id']
    assert db.whole_finalizations.count_documents({}) == 2
    assert client.get('/api/merchants/m2/assets/m1-a/usages').json() == []
    assert count(client) == 1


def test_am_page_identity_and_duplicate_imports_count_one_photo_per_workspace(finalization_app):
    client, db = finalization_app
    db.assets.update_one({'_id': 'm1-a'}, {'$set': {'am_media_library_id': 'shared-media'}})
    duplicate = {**db.assets.find_one({'_id': 'm1-a'}), '_id': 'm1-a-duplicate'}
    db.assets.insert_one(duplicate)
    seed_version(db, 't1', 'both', 'm1', ['m1-a', 'm1-a-duplicate'])
    assert save(client, version='both').status_code == 200
    page = enrich_assets(ScopedDatabase(db, 'm1'), [
        {'id': 'm1-a'}, {'id': 'm1-a-duplicate'}, {'id': 'am:shared-media', 'am_media_library_id': 'shared-media'}])
    assert [item['usage_count'] for item in page] == [1, 1, 1]
    assert enrich_assets(ScopedDatabase(db, 'm2'), [{'id': 'am:shared-media'}])[0]['usage_count'] == 0


def test_busy_and_unselectable_output_cannot_be_finalized(finalization_app):
    client, db = finalization_app
    db.whole_tasks.update_one({'_id': 't1'}, {'$set': {'edit.status': 'running'}})
    assert save(client).status_code == 409
    db.whole_tasks.update_one({'_id': 't1'}, {'$set': {'edit.status': 'idle'}})
    db.whole_versions.update_one({'_id': 't1-v1'}, {'$set': {'outputs.0.selectable': False}})
    assert save(client).status_code == 422


def test_parent_output_identity_does_not_include_unused_parallel_output(finalization_app):
    client, db = finalization_app
    db.assets.insert_one({**db.assets.find_one({'_id': 'm1-output'}), '_id': 'alternate-output'})
    db.whole_versions.update_one({'_id': 't1-v1'}, {'$set': {'outputs': [
        {'id': 'unused', 'asset_id': 'm1-output', 'selectable': True,
            'model_input': {'images': [{'asset_id': 'm1-sibling', 'role': 'reference'}]}},
        {'id': 'used', 'asset_id': 'alternate-output', 'selectable': True,
            'model_input': {'images': [{'asset_id': 'm1-a', 'role': 'reference'}]}}
    ]}})
    seed_version(db, 't1', 'v2', 'm1', [], parent='t1-v1')
    db.whole_versions.update_one({'_id': 'v2'}, {'$set': {'model_input.images': [
        {'asset_id': 'alternate-output', 'role': 'current_design'}]}})
    response = client.get(endpoint() + '?version_id=v2&output_id=result')
    assert response.status_code == 200
    assert [item['id'] for item in response.json()['assets']] == ['m1-a']


def test_later_am_mapping_keeps_original_usage_count_and_time(finalization_app):
    client, db = finalization_app
    final = save(client).json()
    db.assets.update_one({'_id': 'm1-a'}, {'$set': {'am_media_library_id': 'later-media'}})
    assert len(client.get(BASE + '/assets/m1-a/usages').json()) == 1
    assert len(client.get(BASE + '/assets/am:later-media/usages').json()) == 1
    page = enrich_assets(ScopedDatabase(db, 'm1'), [
        {'id': 'm1-a'}, {'id': 'am:later-media', 'am_media_library_id': 'later-media'}])
    assert [item['usage_count'] for item in page] == [1, 1]
    assert save(client).json() == final


def test_deletion_before_final_record_insert_cannot_leave_an_orphan(finalization_app, monkeypatch):
    client, db = finalization_app
    insert = db.whole_finalizations.insert_one
    def interleaved(document, *args, **kwargs):
        assert client.delete(BASE + '/whole-image/tasks/t1/candidate', headers=HEADERS).status_code == 204
        return insert(document, *args, **kwargs)
    monkeypatch.setattr(db.whole_finalizations, 'insert_one', interleaved)
    assert save(client).status_code == 409
    assert db.whole_finalizations.count_documents({}) == 0


def test_save_between_delete_check_and_delete_write_keeps_candidate_accessible(finalization_app, monkeypatch):
    client, db = finalization_app
    update = db.whole_tasks.find_one_and_update
    def interleaved(query, changes, *args, **kwargs):
        if 'candidate_deleted_at' in changes.get('$set', {}):
            assert save(client).status_code == 200
        return update(query, changes, *args, **kwargs)
    monkeypatch.setattr(db.whole_tasks, 'find_one_and_update', interleaved)
    response = client.delete(BASE + '/whole-image/tasks/t1/candidate', headers=HEADERS)
    assert response.status_code == 409
    assert client.get(endpoint()).status_code == 200
    assert not db.whole_tasks.find_one({'_id': 't1'}).get('candidate_deleted_at')


def test_concurrent_target_deletion_restores_previous_finalization(finalization_app, monkeypatch):
    client, db = finalization_app
    original = save(client).json()
    seed_task(db, 't2', 'workspace', 'm1', ['m1-b'])
    replace = db.whole_finalizations.replace_one
    def interleaved(query, document, *args, **kwargs):
        assert client.delete(BASE + '/whole-image/tasks/t2/candidate', headers=HEADERS).status_code == 204
        return replace(query, document, *args, **kwargs)
    monkeypatch.setattr(db.whole_finalizations, 'replace_one', interleaved)
    assert save(client, 't2', ['m1-b'], revision=original['revision']).status_code == 409
    assert client.get(endpoint()).json()['finalization'] == original


def test_replacement_rollback_cannot_restore_an_also_deleted_previous_candidate(finalization_app, monkeypatch):
    client, db = finalization_app
    original = save(client).json()
    seed_task(db, 't2', 'workspace', 'm1', ['m1-b'])
    replace = db.whole_finalizations.replace_one
    def interleaved(query, document, *args, **kwargs):
        assert client.delete(BASE + '/whole-image/tasks/t2/candidate', headers=HEADERS).status_code == 204
        result = replace(query, document, *args, **kwargs)
        assert client.delete(BASE + '/whole-image/tasks/t1/candidate', headers=HEADERS).status_code == 204
        return result
    monkeypatch.setattr(db.whole_finalizations, 'replace_one', interleaved)
    assert save(client, 't2', ['m1-b'], revision=original['revision']).status_code == 409
    assert db.whole_finalizations.count_documents({}) == 0


def test_reuploading_same_photo_preserves_its_usage_count(finalization_app, product_png):
    client, db = finalization_app
    db.assets.update_one({'_id': 'm1-a'}, {'$set': {'sha256': hashlib.sha256(product_png).hexdigest()}})
    assert save(client).status_code == 200
    response = client.post(BASE + '/assets', headers=HEADERS, files={'file': ('a.png', product_png, 'image/png')})
    assert response.status_code == 201
    assert response.json()['id'] == 'm1-a'
    assert response.json()['usage_count'] == 1
