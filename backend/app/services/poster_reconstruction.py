"""Run a v2 reconstruction once and import only a verified editable bundle."""
from __future__ import annotations

from datetime import timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import time
import zipfile

from PIL import Image
from pymongo import ReturnDocument

from app.services import agent_image, editable_projects as projects


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def unpack_bundle(data, *, expected_poster_sha, expected_size, browser_verifier=None):
    if len(data) > projects.MAX_PROJECT_BYTES:
        raise projects.ProjectError('工程包过大')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 350 or sum(item.file_size for item in infos) > 150 * 1024 * 1024:
                raise projects.ProjectError('工程包解压大小超过限制')
            files = {}
            for name in ('project.json', 'editor.html', 'final.png', 'qa-report.json'):
                matches = [item for item in infos if item.filename == name]
                if len(matches) != 1:
                    raise projects.ProjectError(f'工程包缺少唯一的 {name}')
                files[name] = archive.read(matches[0])
        project = projects.validate(json.loads(files['project.json']))
        report = json.loads(files['qa-report.json'])
        scopes = report.get('scopes') or {}
        if report.get('status') != 'PASS' or any(scopes.get(k) != 'PASS' for k in ('technical', 'browser', 'text')) or scopes.get('visual') not in ('PASS', 'PASS_WITH_DIFFERENCES'):
            raise projects.ProjectError('工程尚未通过文字、技术、浏览器和视觉验收')
        expected = {'poster_sha256': expected_poster_sha,
                    'project_sha256': hashlib.sha256(files['project.json']).hexdigest(),
                    'final_sha256': hashlib.sha256(files['final.png']).hexdigest()}
        if any(report.get(key) != digest for key, digest in expected.items()):
            raise projects.ProjectError('工程验收记录与实际文件不一致')
        with Image.open(io.BytesIO(files['final.png'])) as image:
            if image.format != 'PNG' or image.size != expected_size or image.convert('RGBA').getchannel('A').getextrema() != (255, 255):
                raise projects.ProjectError('最终 PNG 尺寸或不透明度不合格')
        if (project['width'], project['height']) != expected_size:
            raise projects.ProjectError('工程画布与原稿不一致')
        if browser_verifier is not None:
            report['system_browser_verification'] = browser_verifier(project, files['editor.html'], files['final.png'])
        return project, files['editor.html'], report
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError) as exc:
        raise projects.ProjectError('无法读取重建工程包') from exc


def _source(db, store, asset_id):
    asset = db.assets.find_one({'_id': asset_id})
    if asset is None or not asset.get('path'):
        raise projects.ProjectError('原始素材不存在')
    return store.get_bytes(asset['path'])


def _references(doc):
    data = doc.get('input_snapshot') or {}
    photos = list(data.get('reference_asset_ids') or data.get('product_asset_ids') or [])
    logos = list(data.get('logo_asset_ids') or [])
    if data.get('logo_asset_id'):
        logos.append(data['logo_asset_id'])
    return list(dict.fromkeys(photos)), list(dict.fromkeys(logos))


def _input(db, settings, store, client, doc):
    photos, logos = _references(doc)
    entries = []
    for role, ids in [('design', [doc['source_asset_id']]), ('reference', photos), ('official_logo', logos)]:
        for asset_id in ids:
            url = agent_image.asset_public_url(db, settings, store, client, asset_id, expires=3600, use_original=True)
            entries.append({'role': role, 'input_asset_id': asset_id,
                            'sha256': hashlib.sha256(_source(db, store, asset_id)).hexdigest(), 'url': url})
    return ('Use image-editor-v2 to reconstruct the selected poster as a fully editable project. '
            'All poster-added text must be native editable text, separate from brush backplates. '
            'Use original photos and official logos exactly as supplied, allowing only framing, uniform scale, '
            'translation and in-plane rotation. Keep captions baked into original photos. '
            'Inventory and separate all artwork objects, restore a clean opaque background. '
            'Run material, text, browser and independent visual checks; fix failed groups and recheck. '
            'Do not report complete unless all checks passed. Deliver one ZIP whose root contains '
            'project.json, editor.html, final.png and qa-report.json. The report must have status PASS, '
            'scopes technical/browser/text=PASS, visual=PASS or PASS_WITH_DIFFERENCES, '
            'and project_sha256/final_sha256/poster_sha256 bound to actual files. '
            'Record source provenance and real generation calls; preserve existing run IDs on retry. '
            'qa-report.json must include source_mapping array: one entry per supplied reference '
            '{input_asset_id, used:boolean, layer_ids:[], reason:...}. Used originals must map to actual photo layers. '
            'Mark a reference unused only when it is a style reference or not visible in the selected design, with a specific reason. '
            'No natural-language editor is required. User facts take precedence over generated copy.\n'
            + json.dumps({'user_requirements': doc.get('input_snapshot') or {}, 'sources': entries}, ensure_ascii=False))


def install_result(db, settings, store, client, doc, result):
    from app.services.editable_project_verification import verify
    candidates = [a for a in result.artifacts if a.file_name.lower().endswith('.zip') or a.content_type.split(';')[0] == 'application/zip']
    if len(candidates) != 1:
        raise projects.ProjectError('Agent 未返回唯一的已验收工程包')
    original = _source(db, store, doc['source_asset_id'])
    with Image.open(io.BytesIO(original)) as image:
        size = image.size
    project, html, report = unpack_bundle(client.download_agent_file(candidates[0].download_url, content_type=candidates[0].content_type),
                                          expected_poster_sha=hashlib.sha256(original).hexdigest(), expected_size=size,
                                          browser_verifier=verify)
    photos, logos = _references(doc)
    allowed = {kind: {hashlib.sha256(_source(db, store, asset_id)).hexdigest() for asset_id in ids}
               for kind, ids in [('photo', photos), ('logo', logos)]}
    for layer in project['layers']:
        kind = layer['type']
        if kind not in allowed:
            continue
        _, raw = projects._decode(project['assets'][layer.get('replacementId') or layer['assetId']])
        if hashlib.sha256(raw).hexdigest() not in allowed[kind]:
            raise projects.ProjectError('照片或 Logo 未使用对应原始素材')
    if logos and not any(layer['type'] == 'logo' for layer in project['layers']):
        raise projects.ProjectError('工程缺少官方 Logo 图层')
    mapping = report.get('source_mapping')
    if photos and (not isinstance(mapping, list) or {item.get('input_asset_id') for item in mapping} != set(photos) or len(mapping) != len(photos)):
        raise projects.ProjectError('工程缺少完整的原素材映射')
    layers = {layer['id']: layer for layer in project['layers']}
    mapped_layers = set()
    for item in mapping or []:
        if type(item.get('used')) is not bool or not isinstance(item.get('reason'), str) or not item['reason'].strip() or not isinstance(item.get('layer_ids'), list):
            raise projects.ProjectError('原素材映射缺少使用说明')
        if item['used'] and not item['layer_ids']:
            raise projects.ProjectError('已使用原照片缺少对应图层')
        for layer_id in item['layer_ids']:
            layer = layers.get(layer_id)
            if layer is None or layer['type'] != 'photo' or projects._decode(project['assets'][layer['assetId']])[1] != _source(db, store, item['input_asset_id']):
                raise projects.ProjectError('原照片映射与实际图层不一致')
            mapped_layers.add(layer_id)
        if not item['used'] and item['layer_ids']:
            raise projects.ProjectError('未使用原照片不能绑定图层')
    if mapped_layers != {layer['id'] for layer in project['layers'] if layer['type'] == 'photo'}:
        raise projects.ProjectError('实际原照片图层缺少已使用映射')
    projects.install(db, settings, store, doc, project, editor_html=html, report=report)


def run(db, settings, store, client, project_id):
    doc = db.whole_editable_projects.find_one_and_update(
        {'_id': project_id, 'status': 'queued'},
        {'$set': {'status': 'running', 'stage': 'preparing', 'updated_at': projects.now()}},
        return_document=ReturnDocument.AFTER)
    if doc is None:
        return
    try:
        deadline = _utc(doc['deadline_at'])
        if projects.now() >= deadline:
            raise projects.ProjectError('已达到 30 分钟处理上限，已有运行记录已保留', 504)
        if not doc.get('run_id'):
            agent_id = settings.core_ai_editable_poster_agent_id
            if not agent_id:
                raise projects.ProjectError('尚未配置 image-editor-v2 Agent', 503)
            text = _input(db, settings, store, client, doc)
            if projects.now() >= deadline:
                raise projects.ProjectError('素材准备已达到 30 分钟上限，未发起新的模型调用', 504)
            db.whole_editable_projects.update_one({'_id': project_id}, {'$set': {'agent_input': text, 'stage': 'triggering'}})
            run_id = client.trigger_agent(agent_id, text, timeout_seconds=max(1, int((deadline - projects.now()).total_seconds())))
            db.whole_editable_projects.update_one({'_id': project_id}, {'$set': {'run_id': run_id, 'stage': 'agent_running'}})
            doc['run_id'] = run_id
        while projects.now() < deadline:
            result = client.poll_agent_detail(doc['run_id'], include_files=True)
            if result is not None:
                install_result(db, settings, store, client, doc, result)
                return
            time.sleep(min(5, max(0, (deadline - projects.now()).total_seconds())))
        raise projects.ProjectError('已达到 30 分钟处理上限，远端可能仍在运行，请查询已有结果', 504)
    except Exception as exc:
        db.whole_editable_projects.update_one({'_id': project_id, 'status': 'running'},
                                             {'$set': {'status': 'failed', 'stage': 'failed', 'error': str(exc), 'updated_at': projects.now()}})


def resume(db, settings, store, client):
    # Runs with unknown IDs are never resubmitted: a trigger may already have succeeded.
    db.whole_editable_projects.update_many({'status': {'$in': ['queued', 'running']}, 'run_id': {'$in': [None, '']}},
        {'$set': {'status': 'interrupted', 'error': '服务重启，运行标识尚未保存，请先核查已有调用'}})
    pending = list(db.whole_editable_projects.find({'status': {'$in': ['queued', 'running']}, 'run_id': {'$nin': [None, '']}}))
    with ThreadPoolExecutor(max_workers=max(1, len(pending))) as executor:
        futures = []
        for doc in pending:
            db.whole_editable_projects.update_one({'_id': doc['_id']}, {'$set': {'status': 'queued'}})
            futures.append(executor.submit(run, db, settings, store, client, doc['_id']))
        for future in futures:
            future.result()
