"""Persist editable poster state separately from immutable generated candidates."""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import re
import uuid
from typing import Any

from PIL import Image

from app.services import storage

MAX_PROJECT_BYTES = 80 * 1024 * 1024
MAX_ASSET_BYTES = 25 * 1024 * 1024
DATA_URI = re.compile(r'^data:(image/(?:png|jpeg|webp)|font/(?:ttf|otf|woff2?|sfnt)|application/(?:font-woff|octet-stream));base64,([A-Za-z0-9+/=\r\n]+)$')


class ProjectError(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def now():
    return datetime.now(timezone.utc)


def _doc(db, project_id, merchant_id):
    doc = db.whole_editable_projects.find_one({'_id': project_id, 'merchant_id': merchant_id})
    if doc is None:
        raise ProjectError('可编辑海报不存在', 404)
    return doc


def create(db, *, task_id, merchant_id, source_version_id, source_output_id, request_id):
    task = db.whole_tasks.find_one({'_id': task_id, 'merchant_id': merchant_id})
    version = db.whole_versions.find_one({'_id': source_version_id, 'task_id': task_id, 'merchant_id': merchant_id})
    if task is None or version is None:
        raise ProjectError('来源海报不存在', 404)
    from app.services.whole_image import _version_outputs
    outputs = _version_outputs(version)
    output = next((item for item in outputs if item.get('id') == source_output_id), None)
    if output is None:
        raise ProjectError('来源候选不存在', 404)
    query = {'task_id': task_id, 'merchant_id': merchant_id, 'request_id': request_id}
    existing = db.whole_editable_projects.find_one(query)
    if existing:
        if (existing['source_version_id'], existing['source_output_id']) != (source_version_id, source_output_id):
            raise ProjectError('同一操作不能用于不同候选', 409)
        return existing
    snapshot = deepcopy(task.get('input') or {})
    references = list(snapshot.get('reference_asset_ids') or snapshot.get('product_asset_ids') or [])
    lineage = []
    seen = set()
    selected = version
    while selected:
        if selected['_id'] in seen or len(seen) >= 200:
            raise ProjectError('来源版本链无效')
        seen.add(selected['_id'])
        lineage.append({'version_id': selected['_id'], 'instruction': selected.get('instruction') or '',
                        'operation': selected.get('operation') or '', 'replacements': selected.get('replacements') or []})
        for item in (selected.get('model_input') or {}).get('images') or []:
            if item.get('asset_id') and item.get('role') not in ('current_poster', 'current_design', 'edit_base', '替换底图', 'logo', 'style_reference'):
                references.append(item['asset_id'])
        references.extend(item['asset_id'] for item in selected.get('replacements') or [] if item.get('asset_id'))
        parent = selected.get('parent_version_id')
        selected = db.whole_versions.find_one({'_id': parent, 'task_id': task_id, 'merchant_id': merchant_id}) if parent else None
        if parent and selected is None:
            raise ProjectError('来源父版本不存在')
    snapshot['reference_asset_ids'] = list(dict.fromkeys(references))
    snapshot['version_lineage'] = list(reversed(lineage))
    doc = {'_id': f'wep_{uuid.uuid4().hex}', **query, 'source_version_id': source_version_id,
           'source_output_id': source_output_id, 'source_asset_id': output.get('asset_id'),
           'input_snapshot': snapshot, 'revision': 0,
           'status': 'queued', 'stage': 'queued', 'created_at': now(), 'updated_at': now(),
           'deadline_at': now() + timedelta(minutes=30)}
    # The compound unique index makes concurrent duplicate clicks idempotent too.
    from pymongo.errors import DuplicateKeyError
    try:
        db.whole_editable_projects.insert_one(doc)
    except DuplicateKeyError:
        return create(db, task_id=task_id, merchant_id=merchant_id, source_version_id=source_version_id,
                      source_output_id=source_output_id, request_id=request_id)
    return doc


def _decode(source: str):
    match = DATA_URI.fullmatch(source) if isinstance(source, str) else None
    if match is None:
        raise ProjectError('工程只能引用内嵌图片和字体')
    try:
        data = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ProjectError('素材编码无效') from exc
    if not data or len(data) > MAX_ASSET_BYTES:
        raise ProjectError('素材大小超过限制')
    if match[1].startswith('image/'):
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 40_000_000:
                    raise ProjectError('图片格式或尺寸无效')
                image.verify()
        except (OSError, ValueError) as exc:
            raise ProjectError('图片数据无效') from exc
    return match[1], data


def validate(project):
    try:
        encoded = json.dumps(project, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (TypeError, ValueError) as exc:
        raise ProjectError('工程数据无效') from exc
    if len(encoded) > MAX_PROJECT_BYTES or not isinstance(project, dict) or project.get('schema') != 'image-editor-v2':
        raise ProjectError('工程格式或大小无效')
    for key in ('width', 'height'):
        v = project.get(key)
        if type(v) is not int or not 0 < v <= 8192:
            raise ProjectError('工程画布无效')
    if project['width'] * project['height'] > 40_000_000:
        raise ProjectError('工程画布过大')
    assets = project.get('assets')
    layers = project.get('layers')
    if not isinstance(assets, dict) or len(assets) > 1000 or not isinstance(layers, list) or not 0 < len(layers) <= 500:
        raise ProjectError('工程图层无效')
    for asset_id, source in assets.items():
        if not isinstance(asset_id, str) or len(asset_id) > 100:
            raise ProjectError('素材标识无效')
        mime, _ = _decode(source)
        if not mime.startswith('image/'):
            raise ProjectError('图片素材类型无效')
    ids = set()
    for layer in layers:
        if not isinstance(layer, dict) or not isinstance(layer.get('id'), str) or layer['id'] in ids:
            raise ProjectError('图层标识重复或无效')
        ids.add(layer['id'])
        if layer.get('type') not in ('image', 'photo', 'text', 'logo', 'background'):
            raise ProjectError('图层类型无效')
        for key in ('x', 'y', 'width', 'height', 'scale', 'rotation', 'opacity'):
            value = layer.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 100_000:
                raise ProjectError('图层数值无效')
        if layer['width'] <= 0 or layer['height'] <= 0 or not 0 < layer['scale'] <= 100 or not 0 <= layer['opacity'] <= 1:
            raise ProjectError('图层尺寸无效')
        if layer['type'] == 'text':
            text = layer.get('text')
            if not isinstance(text, dict) or not isinstance(text.get('value'), str) or len(text['value']) > 5000:
                raise ProjectError('文字数据无效')
        elif layer.get('assetId') not in assets:
            raise ProjectError('图层素材缺失')
        if layer.get('replacementId') and layer['replacementId'] not in assets:
            raise ProjectError('替换素材缺失')
    fonts = project.get('fonts', [])
    if not isinstance(fonts, list) or len(fonts) > 30:
        raise ProjectError('字体列表无效')
    for font in fonts:
        if not isinstance(font, dict) or not isinstance(font.get('family'), str):
            raise ProjectError('字体无效')
        mime, data = _decode(font.get('source'))
        if mime.startswith('image/') or font.get('sha256') != hashlib.sha256(data).hexdigest():
            raise ProjectError('字体来源校验失败')
    from app.services.editable_project_schema import validate_project
    try:
        validate_project(project)
    except (ValueError, TypeError, KeyError) as exc:
        raise ProjectError(str(exc)) from exc
    return deepcopy(project)


def _pack(settings, store, project_id, project):
    value = validate(project)
    def pack(source):
        mime, data = _decode(source)
        digest = hashlib.sha256(data).hexdigest()
        key = storage.object_key(settings, 'artifacts', f'editable/{project_id}/assets/{digest}')
        if not store.exists(key):
            store.put_bytes(key, data, mime)
        return {'key': key, 'mime': mime, 'sha256': digest}
    value['assets'] = {name: pack(source) for name, source in value['assets'].items()}
    for font in value.get('fonts', []):
        font['source'] = pack(font['source'])
    key = storage.object_key(settings, 'artifacts', f'editable/{project_id}/states/{uuid.uuid4().hex}.json')
    store.put_bytes(key, json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'), 'application/json')
    return key


def _unpack(store, key):
    value = json.loads(store.get_bytes(key))
    def unpack(ref):
        data = store.get_bytes(ref['key'])
        if hashlib.sha256(data).hexdigest() != ref['sha256']:
            raise ProjectError('已保存素材校验失败', 500)
        return f"data:{ref['mime']};base64," + base64.b64encode(data).decode()
    value['assets'] = {name: unpack(ref) for name, ref in value['assets'].items()}
    for font in value.get('fonts', []):
        font['source'] = unpack(font['source'])
    return value


def describe(doc):
    return {'id': doc['_id'], **{k: v.isoformat() if isinstance(v, datetime) else v for k, v in doc.items()
            if k in ('task_id', 'source_version_id', 'source_output_id', 'revision', 'status', 'stage',
                     'error', 'created_at', 'updated_at', 'deadline_at', 'run_id')}}


def get(db, store, project_id, merchant_id):
    doc = _doc(db, project_id, merchant_id)
    result = describe(doc)
    if doc.get('project_key'):
        result['project'] = _unpack(store, doc['project_key'])
    return result


def install(db, settings, store, doc, project, *, editor_html, report):
    if report.get('status') != 'PASS':
        raise ProjectError('重建工程尚未通过完整验收')
    key = _pack(settings, store, doc['_id'], project)
    html_key = storage.object_key(settings, 'artifacts', f"editable/{doc['_id']}/editor.html")
    store.put_bytes(html_key, editor_html, 'text/html')
    result = db.whole_editable_projects.update_one(
        {'_id': doc['_id'], 'merchant_id': doc['merchant_id'], 'revision': 0, 'status': {'$in': ['queued', 'running']}},
        {'$set': {'project_key': key, 'initial_project_key': key, 'editor_key': html_key, 'revision': 1,
                  'status': 'ready', 'stage': 'ready', 'updated_at': now(), 'report': report}, '$unset': {'error': ''}})
    if not result.modified_count:
        raise ProjectError('工程状态已改变', 409)


def save(db, settings, store, project_id, merchant_id, expected_revision, project):
    doc = _doc(db, project_id, merchant_id)
    if doc['status'] != 'ready' or doc['revision'] != expected_revision:
        raise ProjectError('工程已更新，请重新打开后再保存', 409)
    current = _unpack(store, doc['project_key'])
    if (project.get('width'), project.get('height')) != (current['width'], current['height']):
        raise ProjectError('不能改变原海报画布')
    key = _pack(settings, store, project_id, project)
    result = db.whole_editable_projects.update_one(
        {'_id': project_id, 'merchant_id': merchant_id, 'revision': expected_revision, 'status': 'ready'},
        {'$set': {'project_key': key, 'updated_at': now(), 'validation_state': 'user_edited'}, '$inc': {'revision': 1}})
    if not result.modified_count:
        store.delete(key)
        raise ProjectError('工程已更新，请重新打开后再保存', 409)
    return describe(_doc(db, project_id, merchant_id))
