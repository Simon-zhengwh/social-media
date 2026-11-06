from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services import agent_tests as service
from app.services.core_ai import CoreAIError

router = APIRouter(prefix='/api/agent-tests', tags=['agent-tests'])


class CreateBatch(BaseModel):
    request_id: UUID
    case_ids: list[str] = Field(min_length=1, max_length=5)
    note: str = Field(default='', max_length=100)


class BatchVisibility(BaseModel):
    hidden: bool = Field(strict=True)


def fail(error):
    raise HTTPException(status_code=error.status_code, detail=error.message) from error


@router.get('/cases')
def cases(request: Request):
    try:
        return service.list_cases(request.app.state.settings)
    except CoreAIError as error:
        fail(error)


@router.get('/cases/{case_id}/images/{index}')
def image(case_id: str, index: int, request: Request, preview: bool = False):
    try:
        return FileResponse(service.image_path(request.app.state.settings, case_id, index, preview=preview),
                            headers={'Cache-Control': 'private, max-age=86400'})
    except CoreAIError as error:
        fail(error)


@router.get('/batches')
def batches(request: Request, include_hidden: bool = False):
    return service.list_batches(request.app.state.db, include_hidden=include_hidden)


@router.patch('/batches/{batch_id}/visibility')
def visibility(batch_id: str, payload: BatchVisibility, request: Request):
    try:
        return service.set_batch_hidden(request.app.state.db, batch_id, payload.hidden)
    except CoreAIError as error:
        fail(error)


@router.post('/batches', status_code=202)
def create(payload: CreateBatch, request: Request, background: BackgroundTasks):
    state = request.app.state
    if not state.settings.core_ai_api_key:
        raise HTTPException(status_code=503, detail='未配置 CORE_AI_API_KEY')
    try:
        result = service.create_batch(state.db, state.settings, str(payload.request_id), payload.case_ids, payload.note)
    except CoreAIError as error:
        fail(error)
    background.add_task(service.run_batch, state.db, state.settings, state.core_ai, result['id'])
    return result


@router.get('/batches/{batch_id}')
def batch(batch_id: str, request: Request):
    try:
        return service.get_batch(request.app.state.db, batch_id)
    except CoreAIError as error:
        fail(error)


@router.post('/batches/{batch_id}/resume', status_code=202)
def resume(batch_id: str, request: Request, background: BackgroundTasks):
    state = request.app.state
    try:
        result = service.get_batch(state.db, batch_id)
    except CoreAIError as error:
        fail(error)
    if any(r['status'] == 'trigger_unknown' for r in result['runs']):
        raise HTTPException(status_code=409, detail='存在结果未知的触发请求，请先在平台确认，不能自动重发')
    background.add_task(service.run_batch, state.db, state.settings, state.core_ai, batch_id)
    return result


@router.get('/runs/{local_run_id}/report')
def report(local_run_id: str, request: Request):
    try:
        path = service.report_path(request.app.state.settings, local_run_id)
    except CoreAIError as error:
        fail(error)
    if not path.is_file():
        raise HTTPException(status_code=404, detail='平台尚未返回运行详情')
    return FileResponse(path, media_type='application/json', filename=local_run_id+'-run.json')


@router.get('/runs/{local_run_id}/artifacts/{index}/preview')
def preview(local_run_id: str, index: int, request: Request):
    state = request.app.state
    try:
        path = service.html_preview(state.db, state.settings, state.core_ai, local_run_id, index)
    except CoreAIError as error:
        fail(error)
    return FileResponse(path, media_type='text/html', headers={
        'Content-Disposition': 'inline', 'Cache-Control': 'private, no-store',
        'Content-Security-Policy': "sandbox allow-scripts allow-downloads; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'",
        'X-Content-Type-Options': 'nosniff'})
