from __future__ import annotations

import hashlib
import uuid
from collections import Counter
from typing import Any
from urllib.parse import urlencode

from pymongo.errors import DuplicateKeyError

from app.api_paths import asset_url
from app.services.asset_records import is_generated_asset
from app.services.whole_image import StudioError, _version_outputs, utcnow
from app.time_utils import utc_iso


def group_key(task: dict[str, Any]) -> str:
    data = task.get('input') or {}
    return (str(data.get('generation_workspace_id') or '').strip()
            or str(data.get('candidate_batch_id') or '').strip() or str(task['_id']))


def _task(db: Any, task_id: str) -> dict[str, Any]:
    task = db.whole_tasks.find_one({'_id': task_id, 'candidate_deleted_at': {'$exists': False}})
    if task is None:
        raise StudioError('task_not_found', '任务不存在', 404)
    return task


def _record_id(task: dict[str, Any]) -> str:
    identity = str(task.get('merchant_id') or '') + '\0' + group_key(task)
    return 'final_' + hashlib.sha256(identity.encode()).hexdigest()


def current(db: Any, task: dict[str, Any]) -> dict[str, Any] | None:
    return db.whole_finalizations.find_one({'_id': _record_id(task)})


def _asset_key(asset: dict[str, Any]) -> str:
    media_id = asset.get('am_media_library_id')
    return 'am:' + str(media_id) if media_id else str(asset.get('id') or asset['_id'])


def _asset_aliases(db: Any, items: list[dict[str, Any]]) -> dict[str, set[str]]:
    ids = [str(item['id']) for item in items]
    documents = {str(item['_id']): item for item in db.assets.find({'_id': {'$in': ids}})}
    media_ids = set()
    for item in items:
        asset_id = str(item['id'])
        media_id = (documents.get(asset_id) or item).get('am_media_library_id')
        if media_id or asset_id.startswith('am:'):
            media_ids.add(str(media_id or asset_id[3:]))
    by_media: dict[str, set[str]] = {media_id: {'am:' + media_id} for media_id in media_ids}
    if media_ids:
        for document in db.assets.find({'am_media_library_id': {'$in': list(media_ids)}}):
            by_media[str(document['am_media_library_id'])].add(str(document['_id']))
    aliases = {}
    for item in items:
        asset_id = str(item['id'])
        key = _asset_key(documents.get(asset_id) or item)
        aliases[asset_id] = {asset_id, key} | by_media.get(key[3:] if key.startswith('am:') else '', set())
    return aliases


def _asset_out(asset: dict[str, Any]) -> dict[str, Any]:
    return {'id': str(asset['_id']), 'original_name': str(asset.get('original_name') or '图片'),
        'width': int(asset.get('width') or 0), 'height': int(asset.get('height') or 0),
        'mime_type': str(asset.get('mime_type') or 'image/png'), 'created_at': utc_iso(asset.get('created_at')),
        'url': asset_url(str(asset.get('merchant_id') or ''), str(asset['_id']))}


def _source(db: Any, task_id: str, version_id: str, output_id: str) -> tuple[dict, dict]:
    version = db.whole_versions.find_one({'_id': version_id, 'task_id': task_id})
    if version is None:
        raise StudioError('version_not_found', '海报版本不存在', 404)
    output = next((item for item in _version_outputs(version) if item.get('id') == output_id and item.get('selectable')), None)
    if output is None or not output.get('asset_id'):
        raise StudioError('output_not_found', '请选择可用的海报结果', 422)
    if db.assets.find_one({'_id': output['asset_id']}) is None:
        raise StudioError('output_missing', '海报图片已不存在', 422)
    return version, output


def _photos(db: Any, task: dict, version: dict, output: dict) -> list[dict]:
    ids: list[str] = []
    logos = {str((task.get('input') or {}).get('logo_asset_id') or '')}
    logos.update(str(item) for item in (task.get('brand') or {}).get('logo_asset_ids') or [])
    seen: set[str] = set()
    while version:
        if version['_id'] in seen:
            raise StudioError('invalid_lineage', '海报版本来源关系异常', 422)
        seen.add(version['_id'])
        snapshot = output.get('model_input') or version.get('model_input') or {}
        images = snapshot.get('images') or []
        for image in images:
            if image.get('role') == 'logo':
                logos.add(str(image.get('asset_id') or ''))
            elif image.get('role') != 'current_design' and image.get('asset_id'):
                ids.append(str(image['asset_id']))
        ids.extend(str(item['asset_id']) for item in version.get('replacements') or [] if item.get('asset_id'))
        parent_id = version.get('parent_version_id')
        if not parent_id:
            if not images:
                data = task.get('input') or {}
                for key in ('reference_asset_ids', 'product_asset_ids', 'style_reference_asset_ids'):
                    ids.extend(data.get(key) or [])
            break
        parent = db.whole_versions.find_one({'_id': parent_id, 'task_id': task['_id']})
        if parent is None:
            raise StudioError('missing_lineage', '海报来源版本缺失，无法整理素材清单', 422)
        source_asset = next((item.get('asset_id') for item in images if item.get('role') == 'current_design'), None)
        parents = _version_outputs(parent)
        output = next((item for item in parents if source_asset and item.get('asset_id') == source_asset),
            next((item for item in parents if item.get('id') == version.get('source_output_id')),
                next((item for item in parents if item.get('selectable')), {})))
        version = parent
    documents = {str(item['_id']): item for item in db.assets.find({'_id': {'$in': list(dict.fromkeys(ids))}})}
    photos = {}
    for asset_id in ids:
        asset = documents.get(asset_id)
        if asset is not None and asset_id not in logos and not is_generated_asset(asset):
            photos.setdefault(_asset_key(asset), asset)
    return list(photos.values())


def record_out(doc: dict[str, Any] | None) -> dict[str, Any] | None:
    if doc is None:
        return None
    merchant_id = str(doc.get('merchant_id') or '')
    return {'id': str(doc['_id']), 'group_key': doc['group_key'], 'task_id': doc['task_id'],
        'version_id': doc['version_id'], 'output_id': doc['output_id'], 'version': doc['version'],
        'asset_ids': doc['asset_ids'], 'revision': doc['revision'], 'finalized_at': utc_iso(doc['finalized_at']),
        'preview_url': asset_url(merchant_id, doc['output_asset_id'], version=doc['version']),
        'task_url': '?' + urlencode({'view': 'whole', 'merchant_id': merchant_id,
            'whole': doc['task_id'], 'whole_workspace': doc['group_key']})}


def context(db: Any, task_id: str, version_id: str | None, output_id: str | None) -> dict[str, Any]:
    task = _task(db, task_id)
    version_id = version_id or task.get('selected_version_id') or task.get('current_version_id')
    photos = []
    if version_id:
        version = db.whole_versions.find_one({'_id': version_id, 'task_id': task_id})
        outputs = _version_outputs(version) if version else []
        output_id = output_id or task.get('current_output_id') or next((item['id'] for item in outputs if item.get('selectable')), '')
        version, output = _source(db, task_id, version_id, output_id)
        photos = _photos(db, task, version, output)
    return {'finalization': record_out(current(db, task)), 'assets': [_asset_out(item) for item in photos]}


def save(db: Any, task_id: str, *, version_id: str, output_id: str,
         expected_revision: str | None) -> dict[str, Any]:
    task = _task(db, task_id)
    if task.get('status') in {'queued', 'running'} or (task.get('edit') or {}).get('status') == 'running':
        raise StudioError('task_busy', '请等待当前海报生成或改图完成')
    version, output = _source(db, task_id, version_id, output_id)
    available = {str(item['_id']): item for item in _photos(db, task, version, output)}
    selected = sorted(available)
    keys = sorted(set(selected) | {_asset_key(available[item]) for item in selected})
    prior = current(db, task)
    if prior and all(prior.get(key) == value for key, value in {
        'task_id': task_id, 'version_id': version_id, 'output_id': output_id, 'asset_ids': sorted(selected)}.items()):
        return record_out(prior)
    if (prior or {}).get('revision') != expected_revision:
        raise StudioError('finalization_changed', '定稿已发生变化，请重新打开后确认')
    doc = {'_id': _record_id(task), 'merchant_id': str(task.get('merchant_id') or ''),
        'group_key': group_key(task), 'task_id': task_id, 'version_id': version_id, 'output_id': output_id,
        'output_asset_id': output['asset_id'], 'version': int(version.get('version') or 1),
        'asset_ids': sorted(selected), 'asset_keys': keys, 'revision': uuid.uuid4().hex, 'finalized_at': utcnow()}
    if prior:
        changed = db.whole_finalizations.replace_one({'_id': prior['_id'], 'revision': expected_revision}, doc)
        if not changed.matched_count:
            raise StudioError('finalization_changed', '定稿已发生变化，请重新打开后确认')
    else:
        try:
            db.whole_finalizations.insert_one(doc)
        except DuplicateKeyError as exc:
            raise StudioError('finalization_changed', '定稿已发生变化，请重新打开后确认') from exc
    # Candidate deletion and this write touch separate documents; both paths verify the postcondition.
    if db.whole_tasks.find_one({'_id': task_id, 'candidate_deleted_at': {'$exists': False}}) is None:
        own_write = {'_id': doc['_id'], 'revision': doc['revision']}
        if prior:
            restored = db.whole_finalizations.find_one_and_replace(own_write, prior)
            if restored and db.whole_tasks.find_one({
                    '_id': prior['task_id'], 'candidate_deleted_at': {'$exists': False}}) is None:
                db.whole_finalizations.delete_one({'_id': prior['_id'], 'revision': prior['revision']})
        else:
            db.whole_finalizations.delete_one(own_write)
        raise StudioError('candidate_deleted', '这张候选已被删除，定稿未保存')
    return record_out(doc)


def revoke(db: Any, task_id: str, expected_revision: str) -> None:
    prior = current(db, _task(db, task_id))
    if prior is None:
        return
    if prior['revision'] != expected_revision or not db.whole_finalizations.delete_one({
            '_id': prior['_id'], 'revision': expected_revision}).deleted_count:
        raise StudioError('finalization_changed', '定稿已发生变化，请重新打开后确认')


def enrich_assets(db: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    aliases = _asset_aliases(db, items)
    keys = set().union(*aliases.values())
    counts: Counter = Counter()
    for doc in db.whole_finalizations.find({'asset_keys': {'$in': list(keys)}}, {'asset_keys': 1}):
        used = set(doc['asset_keys'])
        counts.update(asset_id for asset_id, values in aliases.items() if used & values)
    return [{**item, 'usage_count': counts[item['id']]} for item in items]


def usages(db: Any, asset_id: str) -> list[dict[str, Any]]:
    keys = _asset_aliases(db, [{'id': asset_id}])[asset_id]
    return [record_out(doc) for doc in db.whole_finalizations.find({'asset_keys': {'$in': list(keys)}}).sort('finalized_at', -1)]
