"""Run one phase of an analysis and record it in the analysis record.

The service starts run_phase in a background thread of the web process. Every write checks that
the operation is still the analysis's active one, so a thread that outlived its deadline cannot
overwrite a newer operation. A failed phase records only its calls and error (no evidence, no
partial state); the analyst retries it from the same inputs.
"""
import time

from app.shared.llm import LLMCallError, Tracer, create_llm_client
from app.shared.runs.executor import fenced
from app.shared.util import redact_secrets

from .calls import digest
from .exports import build
from .graph import build_graph
from .storage import Conflict

# Evidence versions each phase publishes when it succeeds, in order: (stage, state field).
PUBLISHED_EVIDENCE = {'extract': (('extraction', 'evidence_v1'), ('refinement', 'evidence')),
                      'feedback': (('feedback', 'evidence'),), 'report': ()}


def llm_provider():
    """The provider Seasonal's calls go through; tests replace it."""
    return None  # The shared factory resolves the configured adapter; tests may inject a replacement.


def _update(service, run_id, operation_id, change, expected='running'):
    return fenced(service.store, run_id, lambda current: change(current, current['operations'][operation_id]),
                  holds=lambda current: (current.get('active') == operation_id
                                         and current['operations'][operation_id]['status'] == expected),
                  refused='This phase is no longer the active operation of the analysis', clock=service.clock)


def describe(exc):
    """A phase error as the analyst reads it: the underlying error of a failed call, credentials redacted."""
    cause = exc.__cause__ if isinstance(exc, LLMCallError) and exc.__cause__ is not None else exc
    return type(cause).__name__ + ': ' + redact_secrets(str(cause))[:1500]


class Recorder:
    """The audit of the operation's model calls: stores each attempt's request and response and keeps its call log.

    The operation's LLM client calls it for every attempt; prepare() gives it the stage's request as the
    analysis record keeps it.
    """

    def __init__(self, service, run_id, operation_id, timeout):
        self.service, self.run_id, self.operation_id, self.timeout = service, run_id, operation_id, timeout
        self.request = None
        self.started = None

    def _write(self, change):
        _update(self.service, self.run_id, self.operation_id, change)

    def elapsed(self):
        return time.monotonic() - self.started if self.started else 0

    def prepare(self, request):
        self.request = request

    def requested(self, record, _llm_request):
        request = self.request
        call = dict(stage=request['stage'], status='calling', started_at=self.service.clock(), model=record.model, provider=record.provider,
                    location=record.location, parameters=record.parameters, timeout_seconds=record.configured_timeout_seconds, call_id=record.call_id,
                    attempt=record.attempt, prompt_version=request['prompt_version'],
                    contract_version=request['contract_version'], prompt_hash=digest(request['system']),
                    contract_schema_hash=record.contract_schema_hash, transport_schema_hash=record.transport_schema_hash, request=self.service.store.put_json(self.run_id, request))
        self._write(lambda run, operation: operation['calls'].append(call))
        self.started = time.monotonic()

    def responded(self, record, response):
        duration = self.elapsed()
        diagnostic = dict(model=response.model_version or record.model, requested_model=record.model,
                          provider=record.provider, location=record.location, parameters=record.parameters,
                          started_at=record.started_at, duration_seconds=duration,
                          timeout_seconds=self.timeout, attempt=record.attempt,
                          prompt_hash=digest(self.request['system']),
                          contract_schema_hash=record.contract_schema_hash, transport_schema_hash=record.transport_schema_hash,
                          token_usage=dict(response.usage), response_id=response.response_id)
        reference = self.service.store.put_json(self.run_id, dict(text=response.text, outcome=response.outcome, finish_reason=response.finish_reason,
                                                                  raw=response.raw, diagnostic=diagnostic))

        def save(run, operation):
            operation['responses'].append(reference)
            operation['calls'][-1].update(diagnostic)
            operation['calls'][-1].update(status='response_saved', response=reference,
                                          outcome=response.outcome, finish_reason=response.finish_reason, duration_seconds=duration)
        self._write(save)

    def validated(self, record):
        stage = self.request['stage']

        def done(run, operation):
            operation['calls'][-1]['status'] = 'validated'
            operation['completed'].append(stage)
        self._write(done)
        self.started = None

    def failed(self, record):
        duration = self.elapsed()
        error = record.error_type + ': ' + record.error_message

        def mark(run, operation):
            operation['calls'][-1].update(status='failed', failure_code=record.failure_code, error=error,
                                          duration_seconds=duration)
        self._write(mark)
        self.started = None


def run_phase(service, run_id, operation_id, provider=None):
    """Run the operation's phase to its end in the calling thread; re-raise its failure."""
    service.get(run_id)  # Refuse previous runtime records before reserving or writing an operation.
    def start(run, op):
        op.update(status='running', started_at=service.clock())
        run['status'] = 'running'
    # A second start of the same operation stops here, before any inference.
    operation = _update(service, run_id, operation_id, start, expected='queued')['operations'][operation_id]
    recorder = Recorder(service, run_id, operation_id, operation['timeout'])
    try:
        # Seasonal's audit already keeps every request and response, so the trace never captures payloads.
        tracer = Tracer(service='seasonal-outlook', run_id=run_id, audit=recorder, capture_payloads=False)
        llm = create_llm_client('seasonal-outlook', settings=service.settings, timeout=operation['timeout'],
                                tracer=tracer, provider=provider or llm_provider(), store=service.store)
        _update(service, run_id, operation_id, lambda run, op: op.update(runtime=llm.describe()))
        graph = build_graph(llm, recorder, timeout=operation['timeout'], namespace=operation_id[:8],
                            export=lambda state: build(state, service.get(run_id), service.store))
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
        error = describe(exc)

        def fail(run, op):
            op.update(status='failed', error=error, finished_at=service.clock())
            # A call the recorder has not closed was interrupted by an error outside the model call.
            if op['calls'] and op['calls'][-1]['status'] not in ('validated', 'failed'):
                op['calls'][-1].update(status='failed', error=error, duration_seconds=recorder.elapsed())
            run.update(active=None, status='failed')
        try:
            _update(service, run_id, operation_id, fail)
        except Conflict:
            pass  # A superseded phase publishes neither success nor failure over its successor.
        raise
