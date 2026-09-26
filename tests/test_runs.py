"""The shared run infrastructure: the store's transactions and the executor's rules for late or dead work."""
import logging
import threading

import pytest

from app.shared.runs.executor import Superseded, expire_overdue, fenced, launch
from app.shared.runs.store import Conflict, MemoryStore, Missing


def record_store(**record):
    store = MemoryStore('runs', missing='Run not found')
    store.mutate('run-1', lambda _current: {'revision': 1, **record})
    return store


def test_fenced_writes_only_while_the_work_holds_the_run():
    store = record_store(status='running', value=0)
    updated = fenced(store, 'run-1', lambda record: record.update(value=1),
                     holds=lambda record: record['status'] == 'running', refused='gone', clock=lambda: 42.0)
    assert (updated['value'], updated['revision'], updated['updated_at']) == (1, 2, 42.0)
    store.mutate('run-1', lambda record: {**record, 'status': 'interrupted'})
    with pytest.raises(Superseded, match='gone') as refused:
        fenced(store, 'run-1', lambda record: record.update(value=2),
               holds=lambda record: record['status'] == 'running', refused='gone', clock=lambda: 43.0)
    assert isinstance(refused.value, Conflict) and store.get('run-1')['value'] == 1
    with pytest.raises(Superseded):
        fenced(store, 'no-such-run', lambda record: None, holds=lambda record: True, refused='gone', clock=lambda: 0)


def test_overdue_work_is_interrupted_once_and_finished_work_is_left_alone():
    now = [100.0]
    overdue = lambda record: record['status'] == 'running' and record['deadline'] <= now[0]  # noqa: E731
    interrupt = lambda record: record.update(status='interrupted')  # noqa: E731
    store = record_store(status='running', deadline=50.0)
    record = expire_overdue(store, 'run-1', store.get('run-1'), overdue=overdue, interrupt=interrupt, clock=lambda: now[0])
    assert (record['status'], record['revision']) == ('interrupted', 2)
    assert expire_overdue(store, 'run-1', record, overdue=overdue, interrupt=interrupt, clock=lambda: now[0]) == record

    # The work finished between the read and the transaction: the fresh record is not overdue any more.
    store = record_store(status='running', deadline=50.0)
    stale = store.get('run-1')
    store.mutate('run-1', lambda current: {**current, 'status': 'completed'})
    assert expire_overdue(store, 'run-1', stale, overdue=overdue, interrupt=interrupt,
                          clock=lambda: now[0])['status'] == 'completed'


def test_launched_work_runs_in_a_daemon_thread_and_its_errors_are_logged(caplog):
    done = threading.Event()

    def fail():
        done.set()
        raise RuntimeError('work broke')
    with caplog.at_level(logging.ERROR, logger='app.shared.runs.executor'):
        launch(fail, name='test-work')
        assert done.wait(5)
        for _ in range(50):
            if caplog.records:
                break
            threading.Event().wait(0.02)
    assert any('test-work' in record.getMessage() and record.exc_info for record in caplog.records)


def test_missing_records_use_the_store_wording():
    with pytest.raises(Missing, match='Run not found'):
        MemoryStore('runs', missing='Run not found').get('nothing')
