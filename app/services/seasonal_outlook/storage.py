"""The Seasonal analysis record on the app's shared store (`app.shared.runs.store`), with its own names and limits."""
from app.shared.runs import store
from app.shared.runs.store import Conflict, Missing, encode

__all__ = ['CloudStore', 'Conflict', 'MISSING', 'MemoryStore', 'Missing', 'encode']

MISSING = 'Analysis not found'


class MemoryStore(store.MemoryStore):
    """Dependency-injected test store only; never selected by environment configuration."""
    def __init__(self):
        super().__init__('seasonal-outlook', missing=MISSING)


class CloudStore(store.CloudStore):
    def __init__(self, settings):
        if not settings.project or not settings.bucket:
            raise ValueError('Explicit Seasonal project and bucket are required for persistence')
        super().__init__(project=settings.project, database=settings.database, collection=settings.collection,
                         bucket=settings.bucket, prefix=settings.prefix, signer=settings.signer,
                         max_record_bytes=800_000, capacity='Analysis audit capacity reached; create a new analysis',
                         missing=MISSING)
