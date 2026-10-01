"""Neutral storage access for Seasonal analysis records."""
from app.shared.runs.factory import create_store
from app.shared.runs.store import Conflict, Missing, encode


def analysis_store(settings):
    return create_store('seasonal-outlook', settings=settings)
