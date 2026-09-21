"""Seasonal configuration never inherits the workstation's default GCP project."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    project: str = ''
    bucket: str = ''
    database: str = 'vam-llm-async'
    collection: str = 'seasonal_outlook_runs'
    prefix: str = 'seasonal-outlook'
    job: str = ''
    job_region: str = ''
    model: str = 'gemini-3.1-pro-preview'
    location: str = 'global'
    signer: str = ''
    backend: str = 'cloud'

    @classmethod
    def from_env(cls):
        fields = {k: os.environ['SEASONAL_' + k.upper()] for k in
                  ('project', 'bucket', 'database', 'collection', 'prefix', 'job', 'job_region', 'model', 'location', 'signer', 'backend')
                  if 'SEASONAL_' + k.upper() in os.environ}
        return cls(enabled=os.getenv('SEASONAL_DRAFTER_ENABLED', 'true').strip().lower() == 'true', **fields)

    def errors(self):
        errors = [f'SEASONAL_{k.upper()} is required' for k in ('project', 'bucket', 'job', 'job_region', 'signer') if not getattr(self, k)]
        if self.backend != 'cloud':
            errors.append('Only durable cloud storage is supported outside injected tests')
        if self.location != 'global':
            errors.append('Seasonal Gemini location must be global')
        return errors
