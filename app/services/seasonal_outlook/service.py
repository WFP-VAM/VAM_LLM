"""Shared application service for Streamlit and FastAPI; phases run in a background thread."""
import hashlib
import logging
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from .config import Settings
from .storage import CloudStore, Conflict, Missing, encode
from .inputs import prepare, inspect_image, PRODUCTS, regions
from .engine import initial_state, CHAINS
from .commands import Create, Upload, ACTIONS

logger = logging.getLogger(__name__)

WORKFLOW_REVISION = 'seasonal-graph-v1'
# Time a phase may need beyond its model calls: object writes and the Word and ZIP exports.
DEADLINE_MARGIN_SECONDS = 600


class Unavailable(ValueError):
    pass


class Gone(ValueError):
    """An analysis stored by the previous Seasonal workflow, which this version does not read."""


def key(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 128 or not all(c.isalnum() or c in '-_' for c in value):
        raise ValueError('Use an 8–128 character request/run identifier containing letters, digits, hyphens or underscores')
    return value


def ordered(operations):
    """Operations oldest first. Firestore does not keep the insertion order of a map."""
    return sorted(operations.values(), key=lambda operation: operation['created_at'])


class Service:
    def __init__(self, settings, store, launch=None, clock=time.time):
        self.settings, self.store, self.clock = settings, store, clock
        self.launch = launch or self._launch_thread

    def enabled(self):
        if not self.settings.enabled:
            raise Unavailable('Seasonal processing is disabled. History and downloads remain available.')
        errors = self.settings.errors()
        if errors:
            raise Unavailable('; '.join(errors))

    def info(self):
        errors = self.settings.errors()
        return dict(service='seasonal-outlook', enabled=self.settings.enabled and not errors,
                    configuration_errors=errors, model=self.settings.model, location=self.settings.location,
                    regions=regions(), products=PRODUCTS, limits=dict(maps=12, file_bytes=30_000_000, total_bytes=50_000_000, pixels=45_000_000))

    def get(self, run_id):
        key(run_id)
        run = self.store.get(run_id)
        if run.get('workflow_revision') != WORKFLOW_REVISION:
            raise Gone('This analysis was created by a previous version of the Seasonal Outlook Drafter and '
                       'can no longer be opened. Create a new analysis.')
        if run.get('active'):
            op = run['operations'][run['active']]
            if op['deadline'] <= self.clock():
                # The phase's thread is gone, e.g. because the server restarted, or it is late.
                def expire(current):
                    operation = current['operations'][op['id']]
                    if current.get('active') == op['id'] and operation['deadline'] <= self.clock():
                        operation.update(status='interrupted', finished_at=self.clock(),
                            error='The phase did not finish before its deadline, for example because the server restarted. Retry it.')
                        current.update(active=None, status='interrupted', revision=current['revision']+1, updated_at=self.clock())
                    return current
                run = self.store.mutate(run_id, expire)
        return run

    def list(self, **filters):
        limit = min(max(int(filters.pop('limit', 30)), 1), 100)
        before = filters.pop('before', None)
        if before:
            before = float(before)
        if set(filters) - {'region_id', 'report_date', 'status'}:
            raise ValueError('Unknown history filter')
        # Analyses of the previous workflow stay listed, but opening one explains that it is gone.
        return [self.get(r['id']) if r.get('workflow_revision') == WORKFLOW_REVISION else r
                for r in self.store.list(limit=limit, before=before, **filters)]

    def _edit(self, run_id, request_id, expected_revision, payload, apply):
        self.enabled()
        key(request_id)
        fingerprint = hashlib.sha256(encode(payload)).hexdigest()

        def update(current):
            if current is None:
                raise Missing('Analysis not found')
            if request_id in current['requests']:
                if current['requests'][request_id]['fingerprint'] != fingerprint:
                    raise Conflict('Request identifier was already used for different input')
                return current
            if current['revision'] != expected_revision:
                raise Conflict('This analysis changed. Refresh the view before continuing.')
            if len(current['requests']) >= 500:
                raise ValueError('Analysis audit capacity reached; create a new analysis')
            current = apply(current)
            current['requests'][request_id] = dict(fingerprint=fingerprint, operation_id=payload.get('operation_id'))
            current.update(revision=current['revision']+1, updated_at=self.clock())
            return current
        return self.store.mutate(run_id, update)

    def create(self, body):
        body = Create.model_validate(body).model_dump()
        self.enabled()
        request_id = key(body['request_id'])
        if body.get('expected_revision') != 0:
            raise Conflict('New analyses require expected_revision=0')
        run_id = hashlib.sha256(('seasonal:' + request_id).encode()).hexdigest()[:32]
        pack = prepare(body['region_id'], body['report_date'], body.get('notes', ''), run_id, [])
        fingerprint = hashlib.sha256(encode(body)).hexdigest()

        def create(current):
            if current:
                if current['creation_fingerprint'] != fingerprint:
                    raise Conflict('Request identifier was already used for different input')
                return current
            return dict(id=run_id, workflow_revision=WORKFLOW_REVISION, revision=1, created_at=self.clock(), updated_at=self.clock(),
                creation_fingerprint=fingerprint, region_id=body['region_id'], region=pack['region_label'], report_date=body['report_date'],
                notes=body.get('notes', ''), status='preparing', maps=[], versions=[], current_evidence=None,
                confirmation=None, active=None, operations={}, requests={}, artifacts={})
        return self.store.mutate(run_id, create)

    def upload(self, run_id, body, data, filename):
        body = dict(body)
        # Multipart scalar fields arrive as strings; normalize only this field.
        if isinstance(body.get('expected_revision'), str) and body['expected_revision'].isdigit():
            body['expected_revision'] = int(body['expected_revision'])
        body = Upload.model_validate(body).model_dump()
        run = self.get(run_id)
        suffix = inspect_image(data, filename)
        if body.get('product') not in PRODUCTS:
            raise ValueError('Select a valid product category')
        if len(body.get('note', '')) > 10000:
            raise ValueError('Image note exceeds 10,000 characters')
        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[suffix]
        self.enabled()
        obj = self.store.put(run_id, data, mime)
        item = dict(name=Path(filename).name[:200], object=obj, product=body['product'],
                    issue_date=body.get('issue_date') or None, note=body.get('note', ''))

        def add(current):
            if current['status'] != 'preparing':
                raise Conflict('Inputs are immutable after extraction starts; create a new analysis')
            maps = current['maps'] + [item]
            if len(maps) > 12 or sum(m['object']['size'] for m in maps) > 50_000_000:
                raise ValueError('Provide at most 12 maps and 50 MB combined')
            if len({m['object']['sha256'] for m in maps}) != len(maps):
                raise ValueError('Duplicate image')
            prepare(current['region_id'], current['report_date'], current['notes'], run_id, maps)
            current['maps'] = maps
            return current
        return self._edit(run_id, body['request_id'], int(body['expected_revision']), dict(action='upload', item=item), add)

    def validate(self, run_id):
        run = self.get(run_id)
        if not run['maps']:
            raise ValueError('Upload at least one map')
        return prepare(run['region_id'], run['report_date'], run['notes'], run_id, run['maps'])

    def version(self, run_id, version_id):
        run = self.get(run_id)
        version = next((v for v in run['versions'] if v['id'] == version_id), None)
        if version is None:
            raise Missing('Evidence version not found')
        return self.store.json(version['object'])

    def action(self, run_id, action, body):
        self.enabled()
        if action not in ACTIONS:
            raise ValueError('Unknown workflow action')
        # Check existing request identity first so a conflicting reused key is a
        # 409 even when its changed payload no longer passes command validation.
        run = self.get(run_id)
        request_id = key(body['request_id'])
        operation_id = hashlib.sha256(request_id.encode()).hexdigest()[:32]
        payload = dict(action=action, body=body, operation_id=operation_id)
        # A repeated request returns the analysis and starts nothing, even when the
        # first caller lost its connection before receiving the acknowledgement.
        if request_id in run['requests']:
            try:
                normalized = ACTIONS[action].model_validate(body).model_dump()
            except ValueError as exc:
                raise Conflict('Request identifier was already used for different input') from exc
            payload['body'] = normalized
            return self._edit(run_id, request_id, body['expected_revision'], payload, lambda r: r)
        body = ACTIONS[action].model_validate(body).model_dump()
        payload['body'] = body
        if run['active']:
            raise Conflict('An operation is already active on this analysis')
        if run['revision'] != body['expected_revision']:
            raise Conflict('This analysis changed. Refresh the view before continuing.')
        timeout = int(body.get('timeout', 600))
        if timeout not in (600, 1200, 1800) or (action != 'retry' and timeout != 600):
            raise ValueError('Phases start with 600 seconds per call; a retry may use 600, 1200 or 1800')
        confirmation, retry_of = None, None
        if action == 'extract':
            if run['status'] != 'preparing':
                raise Conflict('Extraction was already started; retry the failed operation instead')
            pack = self.validate(run_id)
            phase, state = 'extract', initial_state(pack, run['maps'])
        elif action == 'feedback':
            if run['status'] not in ('awaiting_review', 'completed') or body.get('version_id') != run['current_evidence']:
                raise Conflict('Select the latest reviewable evidence version')
            comments = body['comments']
            if not comments.strip() or len(comments) > 20000:
                raise ValueError('Supply between 1 and 20,000 characters of analyst feedback')
            state = initial_state(self.validate(run_id), run['maps'])
            state.update(evidence=self.version(run_id, run['current_evidence']), analyst_comments=comments)
            phase = 'feedback'
        elif action == 'confirm':
            if run['status'] != 'awaiting_review' or body.get('version_id') != run['current_evidence'] or body.get('confirmed') is not True:
                raise Conflict('Explicitly confirm the latest evidence version before drafting')
            state = initial_state(self.validate(run_id), run['maps'])
            state['evidence'] = self.version(run_id, run['current_evidence'])
            # Refuse unsupported title-only reports before spending inference tokens.
            from .science.report_contract import draft_schema
            draft_schema(state)
            confirmation = dict(version_id=run['current_evidence'], evidence_sha256=hashlib.sha256(encode(state['evidence'])).hexdigest(),
                                confirmed_at=self.clock(), request_id=request_id)
            phase = 'report'
        else:
            # A retry runs the whole phase again from the inputs of the latest operation.
            operations = ordered(run['operations'])
            if not operations or operations[-1]['id'] != body['operation_id'] or operations[-1]['status'] not in ('failed', 'interrupted'):
                raise Conflict('Only the latest operation can be retried, and only after it failed or was interrupted')
            old = operations[-1]
            phase, retry_of, state = old['phase'], old['id'], self.store.json(old['input'])
            if phase == 'report':
                confirmation = run['confirmation']
                if not confirmation or confirmation['version_id'] != run['current_evidence'] or confirmation['evidence_sha256'] != hashlib.sha256(encode(state['evidence'])).hexdigest():
                    raise Conflict('The report phase no longer matches the confirmed evidence')
            elif run['current_evidence'] != old['source_evidence']:
                raise Conflict('A newer evidence version exists; use that version for feedback')
        stages = CHAINS[phase]
        initial = self.store.put_json(run_id, state)
        input_artifact = None
        if action == 'extract':
            from .exports import package
            input_artifact = self.store.put(run_id, package(state, run, self.store), 'application/zip')
        launch_id, created = uuid.uuid4().hex, self.clock()
        operation = dict(id=operation_id, phase=phase, action=action, retry_of=retry_of, stages=list(stages), completed=[],
            status='queued', created_at=created, deadline=created + len(stages) * timeout + DEADLINE_MARGIN_SECONDS,
            timeout=timeout, input=initial, responses=[], calls=[], source_evidence=run['current_evidence'],
            confirmation=confirmation, launch_id=launch_id)

        def reserve(current):
            if current['active']:
                raise Conflict('An operation is already active')
            current['operations'][operation_id] = operation
            current.update(active=operation_id, status='queued', confirmation=confirmation)
            # Prior exports stay in the operation history, but never masquerade as
            # the result of a newly revised/confirmed evidence version.
            current['artifacts'] = {}
            if input_artifact:
                current['input_artifact'] = input_artifact
            return current
        result = self._edit(run_id, request_id, body['expected_revision'], payload, reserve)
        # Concurrent copies of one request store a single reservation; only its author starts it.
        if result['operations'][operation_id]['launch_id'] != launch_id:
            return result
        try:
            self.launch(run_id, operation_id)
        except Exception as exc:
            def not_started(current):
                op = current['operations'][operation_id]
                if current.get('active') == operation_id and op['status'] == 'queued':
                    op.update(status='failed', finished_at=self.clock(),
                              error=f'The phase could not start ({type(exc).__name__}). Retry it.')
                    current.update(active=None, status='failed', revision=current['revision']+1, updated_at=self.clock())
                return current
            return self.store.mutate(run_id, not_started)
        return result

    def _launch_thread(self, run_id, operation_id):
        from .runner import run_phase

        def run():
            try:
                run_phase(self, run_id, operation_id)
            except Exception:
                logger.exception('Seasonal operation %s of analysis %s stopped', operation_id, run_id)
        threading.Thread(target=run, name='seasonal-' + operation_id[:8], daemon=True).start()

    def artifact(self, run_id, name, operation_id=None):
        run = self.get(run_id)
        artifacts = run['operations'].get(operation_id, {}).get('artifacts', {}) if operation_id else run['artifacts']
        ref = artifacts.get(name)
        if name == 'input-package.zip' and not operation_id:
            ref = run.get('input_artifact')
        if ref is None:
            raise Missing('Artifact not available')
        return ref


@lru_cache(maxsize=1)
def get_service():
    settings = Settings.from_env()
    if not settings.project or not settings.bucket:
        raise Unavailable('Seasonal durable storage is not configured: set SEASONAL_PROJECT and SEASONAL_BUCKET')
    return Service(settings, CloudStore(settings))


def service_info():
    # Informative even in deployments where Seasonal has not been configured.
    settings = Settings.from_env()
    return Service(settings, None).info()
