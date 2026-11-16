from __future__ import annotations

from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.request_context import request_db, request_store, request_merchant_id
from app.services import editable_projects as projects, poster_reconstruction

router = APIRouter(prefix='/whole-image', tags=['editable-projects'])


class CreateProject(BaseModel):
    source_version_id: str = Field(min_length=1, max_length=100)
    source_output_id: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=1, max_length=100)


class SaveProject(BaseModel):
    expected_revision: int = Field(ge=1)
    project: dict[str, Any]


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except projects.ProjectError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get('/tasks/{task_id}/editable-projects')
def list_projects(task_id: str, request: Request):
    db, merchant_id = request_db(request), request_merchant_id(request)
    if db.whole_tasks.find_one({'_id': task_id, 'merchant_id': merchant_id}) is None:
        raise HTTPException(404, '海报任务不存在')
    return [projects.describe(doc) for doc in db.whole_editable_projects.find({'task_id': task_id, 'merchant_id': merchant_id}).sort('created_at', -1)]


@router.post('/tasks/{task_id}/editable-projects', status_code=202)
def create_project(task_id: str, body: CreateProject, request: Request, background: BackgroundTasks):
    if not request.app.state.settings.core_ai_editable_poster_agent_id:
        raise HTTPException(503, '尚未配置 image-editor-v2 Agent')
    db, store = request_db(request), request_store(request)
    doc = _call(projects.create, db, task_id=task_id, merchant_id=request_merchant_id(request), **body.model_dump())
    if doc['status'] == 'queued':
        background.add_task(poster_reconstruction.run, db, request.app.state.settings, store, request.app.state.core_ai, doc['_id'])
    return projects.describe(doc)


@router.get('/editable-projects/{project_id}')
def get_project(project_id: str, request: Request):
    return _call(projects.get, request_db(request), request_store(request), project_id, request_merchant_id(request))


@router.put('/editable-projects/{project_id}')
def save_project(project_id: str, body: SaveProject, request: Request):
    return _call(projects.save, request_db(request), request.app.state.settings, request_store(request), project_id,
                 request_merchant_id(request), body.expected_revision, body.project)


@router.get('/editable-projects/{project_id}/preview')
def preview_project(project_id: str, request: Request):
    from app.routes.whole_image import ARTIFACT_PREVIEW_HEADERS
    doc = _call(projects._doc, request_db(request), project_id, request_merchant_id(request))
    if doc.get('status') != 'ready' or not doc.get('editor_key'):
        raise HTTPException(409, '工程尚未准备好')
    return Response(request_store(request).get_bytes(doc['editor_key']), media_type='text/html', headers=ARTIFACT_PREVIEW_HEADERS)


@router.post('/editable-projects/{project_id}/refresh', status_code=202)
def refresh_project(project_id: str, request: Request):
    db, store = request_db(request), request_store(request)
    doc = _call(projects._doc, db, project_id, request_merchant_id(request))
    if doc['status'] not in ('ready', 'queued', 'running'):
        if not doc.get('run_id'):
            raise HTTPException(409, '没有已知运行记录，不能重复触发模型')
        # Query an existing result once after timeout, without resetting the original deadline.
        result = request.app.state.core_ai.poll_agent_detail(doc['run_id'], include_files=True)
        if result is not None:
            claimed = db.whole_editable_projects.update_one({'_id': project_id, 'revision': 0, 'status': doc['status']},
                                                            {'$set': {'status': 'running'}})
            if not claimed.modified_count:
                raise HTTPException(409, '工程状态已改变，请刷新')
            try:
                _call(poster_reconstruction.install_result, db, request.app.state.settings, store, request.app.state.core_ai, doc, result)
            except Exception as exc:
                db.whole_editable_projects.update_one({'_id': project_id, 'status': 'running', 'revision': 0},
                    {'$set': {'status': 'failed', 'stage': 'failed', 'error': str(exc), 'updated_at': projects.now()}})
                raise
    return projects.describe(_call(projects._doc, db, project_id, request_merchant_id(request)))
