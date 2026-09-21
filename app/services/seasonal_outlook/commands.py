"""Validate transport input before object uploads, transactions or Job dispatch."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

Identifier = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_-]{8,128}$')]


class Command(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: Identifier
    expected_revision: int = Field(strict=True, ge=0)


class Create(Command):
    region_id: str = Field(min_length=1, max_length=100)
    report_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    notes: str = Field(default='', max_length=10000)


class Upload(Command):
    product: Literal['observed', 'short_term', 'mixed', 'seasonal_rain', 'seasonal_temperature', 'other']
    issue_date: str | None = None
    note: str = Field(default='', max_length=10000)


class Extract(Command):
    timeout: Literal[600] = 600


class Feedback(Extract):
    version_id: Identifier
    comments: str = Field(min_length=1, max_length=20000)


class Confirm(Extract):
    version_id: Identifier
    confirmed: StrictBool


class Resume(Command):
    operation_id: Identifier
    stage: Literal['extraction', 'review', 'refinement', 'feedback', 'draft', 'report_review', 'redraft'] | None = None
    timeout: Literal[600, 1200, 1800] = 600


ACTIONS = dict(extract=Extract, feedback=Feedback, confirm=Confirm, resume=Resume)
