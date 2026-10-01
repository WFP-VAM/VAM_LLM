"""Storage selection and deployment compatibility, invisible to application services."""
import os
from functools import lru_cache
from .store import CloudStore, MemoryStore


def inline_download_limit(service):
    return 20_000_000


def create_store(service, *, settings=None):
    if service == "seasonal-outlook":
        from app.shared.seasonal import SeasonalSettings
        settings = settings or SeasonalSettings.from_env()
        if settings.backend != "cloud" or not settings.project or not settings.bucket:
            raise ValueError("Seasonal durable storage is not configured: set SEASONAL_PROJECT and SEASONAL_BUCKET")
        return CloudStore(project=settings.project, database=settings.database, collection=settings.collection,
                          bucket=settings.bucket, prefix=settings.prefix, signer=settings.signer,
                          namespace=service, max_record_bytes=800_000,
                          capacity="Analysis audit capacity reached; create a new analysis", missing="Analysis not found")
    if service not in {"mfi-drafter", "market-monitor", "report-runs"}:
        raise ValueError(f"Unknown store service: {service}")
    backend = (os.getenv("RUNS_BACKEND") or "").strip().lower()
    uri = (os.getenv("RUNS_GCS_URI") or "").strip()
    if backend not in {"", "memory", "firestore_gcs", "firestore", "gcs"}:
        raise ValueError(f"Unknown run storage backend: {backend}")
    durable = backend in {"firestore_gcs", "firestore", "gcs"} or (not backend and bool(uri))
    if not durable:
        return MemoryStore("runs", missing="Run not found", namespace="report-runs")
    from app.shared.cloud import parse_gcs_uri
    bucket, prefix = parse_gcs_uri(uri, name="RUNS_GCS_URI")
    return CloudStore(project=None, database=(os.getenv("RUNS_FIRESTORE_DATABASE") or "").strip() or "vam-llm-async",
                      collection=(os.getenv("RUNS_FIRESTORE_COLLECTION") or "").strip() or "async_runs",
                      bucket=bucket, prefix=prefix or "runs", namespace="report-runs",
                      capacity="The run record is too large to store", missing="Run not found")


@lru_cache(maxsize=None)
def object_store(namespace):
    return create_store(namespace)
