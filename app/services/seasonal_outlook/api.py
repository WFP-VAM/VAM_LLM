"""One route implementation for both application transports."""
from dataclasses import dataclass
from .service import get_service, ordered, service_info, Gone, Unavailable
from .storage import Conflict, Missing


@dataclass
class Reply:
    status: int = 200
    data: object = None
    content: bytes = b''
    mime: str = 'application/json'
    filename: str = ''


def handle(method, parts, body=None, params=None, upload=None):
    body, params = body or {}, params or {}
    try:
        if method == 'GET' and parts == ['info']:
            return Reply(data=service_info())
        service = get_service()
        if parts == ['runs']:
            if method == 'POST':
                return Reply(data=service.create(body))
            if method == 'GET':
                rows = service.list(**params)
                return Reply(data=[{k: r[k] for k in ('id', 'created_at', 'region_id', 'region', 'report_date', 'status', 'revision')} for r in rows])
        if len(parts) >= 2 and parts[0] == 'runs':
            run_id = parts[1]
            rest = parts[2:]
            if method == 'GET':
                if not rest:
                    return Reply(data=service.get(run_id))
                if rest == ['input']:
                    return Reply(data=service.validate(run_id))
                if rest == ['versions']:
                    return Reply(data=service.get(run_id)['versions'])
                if len(rest) == 2 and rest[0] == 'versions':
                    return Reply(data=service.version(run_id, rest[1]))
                if rest == ['operations']:
                    return Reply(data=ordered(service.get(run_id)['operations']))
                if len(rest) == 2 and rest[0] == 'operations':
                    op = service.get(run_id)['operations'].get(rest[1])
                    if op is None:
                        raise Missing('Operation not found')
                    # Only a completed phase has an output; a failed one keeps its calls and error.
                    output = service.store.json(op['output']) if op.get('output') else None
                    return Reply(data=dict(operation=op, output=output))
                if len(rest) == 2 and rest[0] == 'maps':
                    maps = service.get(run_id)['maps']
                    index = int(rest[1])
                    if index < 0 or index >= len(maps):
                        raise Missing('Map not found')
                    ref = maps[index]['object']
                    return Reply(content=service.store.read(ref), mime=ref['mime'])
                if rest == ['input-package']:
                    ref = service.artifact(run_id, 'input-package.zip')
                    return Reply(data=dict(url=service.store.signed_url(ref, 'input-package.zip'), expires_in=600))
                if len(rest) == 2 and rest[0] in ('artifacts', 'download-link'):
                    name = rest[1]
                    ref = service.artifact(run_id, name, params.get('operation_id'))
                    if rest[0] == 'download-link':
                        return Reply(data=dict(url=service.store.signed_url(ref, name), expires_in=600))
                    # Large exports are delivered directly from GCS, never through
                    # Cloud Run's response-size ceiling.
                    if ref['size'] > 20_000_000:
                        raise ValueError('Use the download-link endpoint for this large export')
                    return Reply(content=service.store.read(ref), mime=ref['mime'], filename=name)
            if method == 'POST':
                if rest == ['maps']:
                    if upload is None:
                        raise ValueError('Upload one map using the file field')
                    return Reply(data=service.upload(run_id, body, upload.content, upload.filename))
                if len(rest) == 1 and rest[0] in ('extract', 'feedback', 'confirm', 'retry'):
                    return Reply(data=service.action(run_id, rest[0], body))
        raise Missing('Seasonal endpoint not found')
    except Conflict as exc:
        return Reply(409, {'detail': str(exc)})
    except Missing as exc:
        return Reply(404, {'detail': str(exc)})
    except Gone as exc:
        return Reply(410, {'detail': str(exc)})
    except Unavailable as exc:
        return Reply(503, {'detail': str(exc)})
    except (ValueError, KeyError, TypeError) as exc:
        return Reply(400, {'detail': str(exc)})
