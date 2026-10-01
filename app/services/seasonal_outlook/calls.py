"""Neutral stage requests: domain payload, original images and application schema."""
import hashlib
import json
from app.shared.llm import FilePart, LLMRequest

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def operation_name(stage):
    return f'seasonal_outlook.{stage}.v1'

def llm_request(request, timeout, work_item):
    parts = [json.dumps(request['payload'], ensure_ascii=False)]
    for figure in request['images']:
        parts += [json.dumps({k: figure[k] for k in ('figure_id', 'metadata_note')}),
                  FilePart(reference=figure['object'])]
    return LLMRequest(operation=operation_name(request['stage']), node=request['stage'], system=request['system'],
                      parts=parts, response_schema=request['schema'], json_output=True,
                      timeout_seconds=timeout, fail_on_truncation=True, work_item=work_item)
