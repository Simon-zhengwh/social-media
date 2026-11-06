"""Fixed-input Agent benchmarks with persisted run IDs and bounded parallel execution."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any
import uuid

from pymongo import ReturnDocument

from app.config import Settings
from app.services.core_ai import CoreAIClient, CoreAIError

TERMINAL = {'completed', 'failed', 'cancelled', 'timeout'}
ATTENTION = {'trigger_unknown', 'wait_timeout', 'needs_attention'}
MAX_BATCH_CONCURRENCY = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(settings: Settings) -> Path:
    return settings.storage_path / 'agent-test-cases'


def _catalog(settings: Settings) -> dict:
    path = _root(settings) / 'manifest.json'
    if not path.is_file():
        raise CoreAIError('benchmark_missing', '固定测试素材尚未准备，请运行 prepare_agent_test_cases.py', 503)
    return json.loads(path.read_text(encoding='utf-8-sig'))


def image_path(settings: Settings, case_id: str, index: int, *, preview=False) -> Path:
    case = next((c for c in _catalog(settings)['cases'] if c['id'] == case_id), None)
    if case is None or not 0 <= index < len(case['images']):
        raise CoreAIError('not_found', '测试图片不存在', 404)
    root = _root(settings).resolve()
    path = (root / case['images'][index]['file']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise CoreAIError('benchmark_missing', '基准图片路径无效', 422)
    return path.with_suffix('.jpg') if preview and path.with_suffix('.jpg').is_file() else path


def list_cases(settings: Settings) -> dict:
    catalog = _catalog(settings)
    return {'version': catalog['version'], 'agent_id': settings.core_ai_test_agent_id,
            'server_url': settings.core_ai_server_base_url.rstrip('/'),
            'cases': [{**{k: c[k] for k in ['id', 'label', 'prompt']},
                       'images': [{**{k: im[k] for k in ['name', 'sha256', 'size']},
                                   'preview_url': f'/api/agent-tests/cases/{c["id"]}/images/{i}?preview=true',
                                   'original_url': f'/api/agent-tests/cases/{c["id"]}/images/{i}'}
                                  for i, im in enumerate(c['images'])]} for c in catalog['cases']]}


def _validate_case(settings, case):
    for i, image in enumerate(case['images']):
        if hashlib.sha256(image_path(settings, case['id'], i).read_bytes()).hexdigest() != image['sha256']:
            raise CoreAIError('benchmark_changed', '基准图片发生变化，已阻止请求：'+image['name'], 409)


def create_batch(db: Any, settings: Settings, request_id: str, case_ids: list[str], note: str) -> dict:
    try:
        request_id = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise CoreAIError('invalid_request', '请求标识无效', 422) from exc
    catalog = _catalog(settings)
    lookup = {c['id']: c for c in catalog['cases']}
    if not case_ids or len(case_ids) > 5 or len(set(case_ids)) != len(case_ids) or any(c not in lookup for c in case_ids):
        raise CoreAIError('invalid_case', '请选择有效且不重复的基准用例', 422)
    existing = db.agent_test_batches.find_one({'_id': request_id})
    if existing and (existing['case_ids'] != case_ids or existing['note'] != note):
        raise CoreAIError('request_conflict', '同一请求标识不能用于另一组测试', 409)
    for cid in case_ids:
        _validate_case(settings, lookup[cid])
    db.agent_test_batches.update_one({'_id': request_id}, {'$setOnInsert': {
        'case_ids': case_ids, 'note': note, 'version': catalog['version'], 'created_at': _now(),
        'agent_id': settings.core_ai_test_agent_id,
        'server_url': settings.core_ai_server_base_url.rstrip('/')}}, upsert=True)
    for order, cid in enumerate(case_ids):
        run_id = uuid.uuid5(uuid.UUID(request_id), cid).hex
        db.agent_test_runs.update_one({'_id': run_id}, {'$setOnInsert': {
            'batch_id': request_id, 'order': order, 'case_id': cid, 'label': lookup[cid]['label'],
            'prompt': lookup[cid]['prompt'], 'input_sha256': [im['sha256'] for im in lookup[cid]['images']],
            'agent_id': settings.core_ai_test_agent_id, 'server_url': settings.core_ai_server_base_url.rstrip('/'),
            'status': 'queued', 'created_at': _now(), 'run_id': None, 'trace_id': None,
            'user_input': lookup[cid]['prompt'], 'submitted_input': None, 'remote_input': None,
            'image_urls': [], 'transcript': [], 'artifacts': [], 'output': '', 'error': None}}, upsert=True)
    return get_batch(db, request_id)


def _public_run(doc: dict) -> dict:
    return {'id': str(doc['_id']), **{k: v for k, v in doc.items() if k != '_id'}}


def get_batch(db, batch_id: str) -> dict:
    batch = db.agent_test_batches.find_one({'_id': batch_id})
    if batch is None:
        raise CoreAIError('not_found', '测试批次不存在', 404)
    runs = [_public_run(d) for d in db.agent_test_runs.find({'batch_id': batch_id}).sort('order', 1)]
    status = 'completed' if runs and all(r['status'] in TERMINAL for r in runs) else 'needs_attention' if any(r['status'] in ATTENTION for r in runs) else 'running'
    return {'id': batch_id, **{k: v for k, v in batch.items() if k != '_id'},
            'hidden': batch.get('hidden', False), 'status': status, 'runs': runs}


def list_batches(db, *, include_hidden: bool = False) -> list[dict]:
    query = {} if include_hidden else {'hidden': {'$ne': True}}
    return [{'id': x['_id'], 'created_at': x['created_at'], 'note': x['note'],
             'case_ids': x['case_ids'], 'hidden': x.get('hidden', False)}
            for x in db.agent_test_batches.find(query, {'created_at': 1, 'note': 1, 'case_ids': 1, 'hidden': 1})
            .sort('created_at', -1)]


def set_batch_hidden(db, batch_id: str, hidden: bool) -> dict:
    result = db.agent_test_batches.update_one({'_id': batch_id}, {'$set': {'hidden': hidden}})
    if not result.matched_count:
        raise CoreAIError('not_found', '测试批次不存在', 404)
    return get_batch(db, batch_id)


def _safe(value, settings):
    raw = json.dumps(value, ensure_ascii=False)
    if settings.core_ai_api_key:
        raw = raw.replace(settings.core_ai_api_key, '[REDACTED]')
    raw = re.sub(r'data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+', '[INLINE IMAGE OMITTED]', raw)
    return json.loads(raw)


def _media_urls(db, settings, client, case):
    urls = []
    for index, image in enumerate(case['images']):
        cache_id = hashlib.sha256((settings.core_ai_server_base_url+'|'+image['sha256']).encode()).hexdigest()
        cached = db.agent_test_media.find_one({'_id': cache_id})
        if cached is None:
            data = image_path(settings, case['id'], index).read_bytes()
            if hashlib.sha256(data).hexdigest() != image['sha256']:
                raise CoreAIError('benchmark_changed', '基准图片发生变化', 409)
            url = client.upload_image_blob(data, content_type='image/png')
            db.agent_test_media.update_one({'_id': cache_id}, {'$set': {'url': url, 'sha256': image['sha256'], 'created_at': _now()}}, upsert=True)
        else:
            url = cached['url']
        urls.append(url)
    return urls


def report_path(settings, local_run_id):
    if not re.fullmatch(r'[a-f0-9]{32}', local_run_id):
        raise CoreAIError('not_found', '测试记录不存在', 404)
    return settings.storage_path / 'agent-tests' / local_run_id / 'run-detail.json'


def html_preview(db, settings, client, local_run_id, index):
    doc = db.agent_test_runs.find_one({'_id': local_run_id})
    artifacts = doc.get('artifacts', []) if doc else []
    if not 0 <= index < len(artifacts):
        raise CoreAIError('not_found', 'HTML 产物不存在', 404)
    artifact = artifacts[index]
    if artifact.get('content_type', '').split(';')[0] != 'text/html' and not artifact.get('file_name', '').lower().endswith(('.html', '.htm')):
        raise CoreAIError('invalid_preview', '该产物不是 HTML', 422)
    url = artifact.get('download_url')
    if not isinstance(url, str):
        raise CoreAIError('invalid_preview', 'HTML 产物缺少下载地址', 422)
    target = report_path(settings, local_run_id).parent / ('preview-'+hashlib.sha256(url.encode()).hexdigest()[:32]+'.html')
    if not target.is_file():
        data = client.read_agent_html(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / ('p-'+uuid.uuid4().hex[:12]+'.tmp')
        temporary.write_bytes(data)
        temporary.replace(target)
    return target


def _save_detail(db, settings, local_id, raw):
    detail = _safe(raw, settings)
    path = report_path(settings, local_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    transcript = []
    for entry in detail.get('transcript') or []:
        if isinstance(entry, dict):
            transcript.append({k: v[:4000] if isinstance(v, str) else v for k, v in entry.items()})
    trace_id = detail.get('trace_id')
    trace_url = settings.core_ai_server_base_url.rstrip('/')+'/traces/'+trace_id if isinstance(trace_id, str) and re.fullmatch('[A-Za-z0-9_-]+', trace_id) else None
    db.agent_test_runs.update_one({'_id': local_id}, {'$set': {
        'remote_status': detail.get('status'), 'trace_id': trace_id, 'trace_url': trace_url,
        'remote_input': detail.get('input'), 'transcript': transcript, 'output': str(detail.get('output') or ''),
        'artifacts': detail.get('artifacts') or [], 'token_usage': detail.get('token_usage') or {},
        'remote_started_at': detail.get('started_at'), 'remote_completed_at': detail.get('completed_at'),
        'last_observed_at': _now(), 'report_url': '/api/agent-tests/runs/'+local_id+'/report'}})
    return detail


def _run_one(db, settings, client, doc):
    local_id = doc['_id']
    def update(**values):
        db.agent_test_runs.update_one({'_id': local_id}, {'$set': values})
    if doc['status'] in TERMINAL:
        return True
    if doc['status'] == 'trigger_unknown':
        return False
    if not doc.get('run_id'):
        claimed = db.agent_test_runs.find_one_and_update({'_id': local_id, 'status': 'queued'},
            {'$set': {'status': 'preparing', 'started_at': _now()}}, return_document=ReturnDocument.AFTER)
        if claimed is None:
            return False
        try:
            case = next(c for c in _catalog(settings)['cases'] if c['id'] == doc['case_id'])
            _validate_case(settings, case)
            image_urls = _media_urls(db, settings, client, case)
            submitted_input = doc['prompt'].rstrip() + '\n\n' + '\n'.join(image_urls)
        except Exception as error:
            update(status='failed', error=_safe(str(error), settings), finished_at=_now())
            return True
        update(status='triggering', submitted_at=_now(), user_input=doc['prompt'],
               submitted_input=submitted_input, image_urls=image_urls)
        try:
            remote_id = client.trigger_agent(doc['agent_id'], submitted_input,
                timeout_seconds=settings.core_ai_test_timeout_seconds)
        except Exception as error:
            update(status='trigger_unknown', error=_safe(str(error), settings))
            return False
        update(run_id=remote_id, status='running')
    else:
        remote_id = doc['run_id']
        update(status='running', error=None)
    deadline = time.monotonic()+settings.core_ai_test_timeout_seconds
    while time.monotonic()<deadline:
        try:
            detail = _save_detail(db, settings, local_id, client.get_agent_run(remote_id))
            status = detail.get('status')
            if status in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'SKIPPED'}:
                update(status={'COMPLETED': 'completed', 'FAILED': 'failed', 'CANCELLED': 'cancelled', 'TIMEOUT': 'timeout', 'SKIPPED': 'failed'}[status],
                       error=detail.get('error'), finished_at=_now())
                return True
            if status not in {'PENDING', 'RUNNING'}:
                update(status='needs_attention', error='平台返回需要处理的状态：'+str(status))
                return False
        except Exception as error:
            update(error=_safe(str(error), settings), last_observed_at=_now())
        time.sleep(3)
    timeout_minutes = max(1, settings.core_ai_test_timeout_seconds // 60)
    update(status='failed', error=f'本地{timeout_minutes}分钟等待到期；可继续查询已有run，不会重新触发Agent')
    return False


def run_batch(db, settings, client: CoreAIClient, batch_id):
    docs = list(db.agent_test_runs.find({'batch_id': batch_id}).sort('order', 1))
    if not docs:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_BATCH_CONCURRENCY, len(docs)),
                            thread_name_prefix='agent-test') as executor:
        return list(executor.map(lambda doc: _run_one(db, settings, client, doc), docs))


def recover(db):
    db.agent_test_runs.update_many({'status': {'$in': ['preparing', 'triggering']}, 'run_id': None},
        {'$set': {'status': 'trigger_unknown', 'error': '服务曾中断且没有runId；请在平台确认，禁止自动重发'}})
    return [b['_id'] for b in db.agent_test_batches.find()
            if db.agent_test_runs.find_one({'batch_id': b['_id'], 'status': {'$in': ['queued', 'running']}})]
