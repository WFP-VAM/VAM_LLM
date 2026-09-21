"""python -m app.services.seasonal_outlook.worker --run ID --operation ID"""
import argparse
import threading
import time
from .service import get_service
from .storage import Conflict
from .provider import VertexProvider, digest
from .engine import request_for, accept
from .exports import build


def execute(service, run_id, operation_id, provider=None):
    owner = service.claim(run_id, operation_id)  # A duplicate dispatch exits before inference.
    stop = threading.Event()
    lost = threading.Event()

    def pulse():
        while not stop.wait(30):
            try:
                service.heartbeat(run_id, operation_id, owner)
            except Exception:
                lost.set()
                return
    heartbeat = threading.Thread(target=pulse, daemon=True)
    heartbeat.start()
    provider = provider or VertexProvider(service.settings)
    call_started = None
    try:
        run = service.get(run_id)
        op = run['operations'][operation_id]
        state = service.store.json(op['initial_state'])
        for index, stage in enumerate(op['stages']):
            if lost.is_set():
                raise Conflict('Worker lease lost')
            request = request_for(stage, state)
            request_ref = service.store.put_json(run_id, request)
            diagnostic = dict(stage=stage, status='calling', started_at=service.clock(),
                model=service.settings.model, location=service.settings.location, timeout_seconds=op['timeout'],
                attempts=1, prompt_version=request['prompt_version'], contract_version=request['contract_version'],
                prompt_hash=digest(request['system']), schema_hash=digest(request['schema']), request=request_ref)
            service.owned(run_id, operation_id, owner, lambda r, o: o['calls'].append(diagnostic))
            call_started = time.monotonic()
            response = provider.complete(request, op['timeout'])
            response_ref = service.store.put_json(run_id, response)

            def save_response(current, operation):
                operation['responses'].append(response_ref)
                operation['calls'][-1].update(response.get('diagnostic', {}))
                operation['calls'][-1].update(status='response_saved', response=response_ref,
                    finish_reason=response.get('finish_reason'), duration_seconds=time.monotonic()-call_started)
            # Save every returned response BEFORE parsing or invoking another model.
            service.owned(run_id, operation_id, owner, save_response)
            state = accept(stage, state, response, operation_id[:8] + '_' + stage)
            checkpoint = service.store.put_json(run_id, state)
            evidence = state.get('evidence_v1' if stage == 'extraction' else 'evidence') if stage in ('extraction', 'refinement', 'feedback') else None
            evidence_ref = service.store.put_json(run_id, evidence) if evidence else None

            def commit(current, operation):
                operation['checkpoints'].append(checkpoint)
                operation['cursor'] = index+1
                operation['calls'][-1]['status'] = 'validated'
                if evidence_ref:
                    vid = operation_id + '_' + stage
                    current['versions'].append(dict(id=vid, stage=stage, operation_id=operation_id,
                        created_at=service.clock(), object=evidence_ref))
                    current['current_evidence'] = vid
                    operation['result_evidence'] = vid
            service.owned(run_id, operation_id, owner, commit)
            call_started = None
        artifacts = {}
        if op['stages'][-1] == 'redraft':
            artifacts = build(state, service.get(run_id), service.store)

        def finish(current, operation):
            operation.update(status='completed', finished_at=service.clock(), artifacts=artifacts)
            current.update(active=None, status='completed' if artifacts else 'awaiting_review', artifacts=artifacts)
        service.owned(run_id, operation_id, owner, finish)
    except Exception as exc:
        def fail(current, operation):
            error = type(exc).__name__ + ': ' + str(exc)[:1500]
            operation.update(status='failed', error=error, finished_at=service.clock())
            if operation['calls'] and operation['calls'][-1]['status'] != 'validated':
                operation['calls'][-1].update(status='failed', error=error,
                    duration_seconds=time.monotonic()-call_started if call_started else 0)
            current.update(active=None, status='failed')
        try:
            service.owned(run_id, operation_id, owner, fail)
        except Conflict:
            pass  # An expired worker cannot publish success OR failure over its successor.
        raise
    finally:
        stop.set()
        heartbeat.join(timeout=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True)
    parser.add_argument('--operation', required=True)
    args = parser.parse_args()
    execute(get_service(), args.run, args.operation)


if __name__ == '__main__':
    main()
