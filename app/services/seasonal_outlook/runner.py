"""Run one phase of an analysis and record it in the analysis record.

The service starts run_phase in a background thread of the web process. Every write checks that
the operation is still the analysis's active one, so a thread that outlived its deadline cannot
overwrite a newer operation. A failed phase records only its calls and error (no evidence, no
partial state); the analyst retries it from the same inputs.
"""
import time

from .exports import build
from .graph import build_graph
from .provider import VertexProvider, digest
from .storage import Conflict

# Evidence versions each phase publishes when it succeeds, in order: (stage, state field).
PUBLISHED_EVIDENCE = {'extract': (('extraction', 'evidence_v1'), ('refinement', 'evidence')),
                      'feedback': (('feedback', 'evidence'),), 'report': ()}


def _update(service, run_id, operation_id, change, expected='running'):
    def update(current):
        operation = current['operations'][operation_id]
        if current.get('active') != operation_id or operation['status'] != expected:
            raise Conflict('This phase is no longer the active operation of the analysis')
        change(current, operation)
        current.update(revision=current['revision'] + 1, updated_at=service.clock())
        return current
    return service.store.mutate(run_id, update)


class Recorder:
    """Stores each model request and response and keeps the operation's call log."""

    def __init__(self, service, run_id, operation_id, timeout):
        self.service, self.run_id, self.operation_id, self.timeout = service, run_id, operation_id, timeout
        self.started = None

    def _write(self, change):
        _update(self.service, self.run_id, self.operation_id, change)

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0

    def requested(self, stage, request):
        settings = self.service.settings
        call = dict(stage=stage, status='calling', started_at=self.service.clock(), model=settings.model,
                    location=settings.location, timeout_seconds=self.timeout, attempts=1,
                    prompt_version=request['prompt_version'], contract_version=request['contract_version'],
                    prompt_hash=digest(request['system']), schema_hash=digest(request['schema']),
                    request=self.service.store.put_json(self.run_id, request))
        self._write(lambda run, operation: operation['calls'].append(call))
        self.started = time.monotonic()

    def responded(self, response):
        reference = self.service.store.put_json(self.run_id, response)
        duration = self.elapsed()

        def save(run, operation):
            operation['responses'].append(reference)
            operation['calls'][-1].update(response.get('diagnostic', {}))
            operation['calls'][-1].update(status='response_saved', response=reference,
                                          finish_reason=response.get('finish_reason'), duration_seconds=duration)
        self._write(save)

    def validated(self, stage):
        def done(run, operation):
            operation['calls'][-1]['status'] = 'validated'
            operation['completed'].append(stage)
        self._write(done)
        self.started = None


def run_phase(service, run_id, operation_id, provider=None):
    """Run the operation's phase to its end in the calling thread; re-raise its failure."""
    def start(run, op):
        op.update(status='running', started_at=service.clock())
        run['status'] = 'running'
    # A second start of the same operation stops here, before any inference.
    operation = _update(service, run_id, operation_id, start, expected='queued')['operations'][operation_id]
    recorder = Recorder(service, run_id, operation_id, operation['timeout'])
    graph = build_graph(provider or VertexProvider(service.settings), recorder, timeout=operation['timeout'],
                        namespace=operation_id[:8], export=lambda state: build(state, service.get(run_id), service.store))
    try:
        state = graph.invoke({**service.store.json(operation['input']), 'phase': operation['phase']})
        output = service.store.put_json(run_id, state)
        versions = [dict(id=f'{operation_id}_{stage}', stage=stage, operation_id=operation_id,
                         created_at=service.clock(), object=service.store.put_json(run_id, state[field]))
                    for stage, field in PUBLISHED_EVIDENCE[operation['phase']]]
        artifacts = state.get('artifacts', {})

        def finish(run, op):
            op.update(status='completed', finished_at=service.clock(), output=output, artifacts=artifacts)
            if versions:
                run['versions'] = run['versions'] + versions
                run['current_evidence'] = op['result_evidence'] = versions[-1]['id']
            run.update(active=None, status='completed' if artifacts else 'awaiting_review', artifacts=artifacts)
        _update(service, run_id, operation_id, finish)
    except Exception as exc:
        error = type(exc).__name__ + ': ' + str(exc)[:1500]

        def fail(run, op):
            op.update(status='failed', error=error, finished_at=service.clock())
            if op['calls'] and op['calls'][-1]['status'] != 'validated':
                op['calls'][-1].update(status='failed', error=error, duration_seconds=recorder.elapsed())
            run.update(active=None, status='failed')
        try:
            _update(service, run_id, operation_id, fail)
        except Conflict:
            pass  # A superseded phase publishes neither success nor failure over its successor.
        raise
