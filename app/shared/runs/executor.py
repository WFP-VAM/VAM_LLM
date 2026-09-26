"""Background work of the app's runs: where it runs, who may still write, and when it counts as interrupted.

Work runs in a daemon thread of this process; another runner (for example a job service) would replace
`launch` alone. Two rules protect a run's record from work that is gone or late:
- `fenced` applies a change only while the work still holds the run, in one transaction;
- `expire_overdue` marks work that missed its deadline as interrupted, so the reader sees a final state.
"""
import logging
import threading

from .store import Conflict

logger = logging.getLogger(__name__)


class Superseded(Conflict):
    """The work writing to a run no longer holds it: it was interrupted, replaced or already ended."""


def launch(work, *, name):
    """Run `work` in a daemon thread; an error it raises is logged, never lost."""
    def run():
        try:
            work()
        except Exception:
            logger.exception('Background work %s stopped', name)
    threading.Thread(target=run, name=name, daemon=True).start()


def fenced(store, run_id, change, *, holds, refused, clock):
    """Apply `change` to the run's record only while `holds(record)`; otherwise raise Superseded(refused)."""
    def update(current):
        if current is None or not holds(current):
            raise Superseded(refused)
        change(current)
        current.update(revision=current.get('revision', 0) + 1, updated_at=clock())
        return current
    return store.mutate(run_id, update)


def expire_overdue(store, run_id, record, *, overdue, interrupt, clock):
    """The run's record, with overdue work marked interrupted by `interrupt(record)` in one transaction."""
    if not overdue(record):
        return record

    def update(current):
        if overdue(current):  # Checked again inside the transaction: the work may have just finished.
            interrupt(current)
            current.update(revision=current.get('revision', 0) + 1, updated_at=clock())
        return current
    return store.mutate(run_id, update)
