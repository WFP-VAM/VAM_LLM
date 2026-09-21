from types import SimpleNamespace
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool
from .api import handle

router = APIRouter()


@router.api_route('/{path:path}', methods=['GET', 'POST'])
async def seasonal_route(path: str, request: Request):
    upload = None
    body = {}
    if request.method == 'POST':
        if request.headers.get('content-type', '').startswith('multipart/form-data'):
            async with request.form(max_files=1, max_fields=8, max_part_size=30_000_000) as form:
                file = form.get('file')
                if file is None or not hasattr(file, 'read'):
                    return JSONResponse({'detail': 'Upload one map using the file field'}, status_code=400)
                data = await file.read(30_000_001)
                if len(data) > 30_000_000:
                    return JSONResponse({'detail': 'Image exceeds 30 MB'}, status_code=413)
                upload = SimpleNamespace(filename=file.filename, content=data)
                body = {k: v for k, v in form.items() if k != 'file'}
        else:
            raw = await request.body()
            if len(raw) > 100_000:
                return JSONResponse({'detail': 'Request exceeds 100 KB'}, status_code=413)
            import json
            try:
                body = json.loads(raw)
            except ValueError:
                return JSONResponse({'detail': 'Invalid JSON'}, status_code=400)
    result = await run_in_threadpool(handle, request.method, path.strip('/').split('/'), body, dict(request.query_params), upload)
    if result.data is not None:
        return JSONResponse(result.data, status_code=result.status)
    headers = {'Content-Disposition': f'attachment; filename="{result.filename}"'} if result.filename else {}
    return Response(result.content, status_code=result.status, media_type=result.mime, headers=headers)
