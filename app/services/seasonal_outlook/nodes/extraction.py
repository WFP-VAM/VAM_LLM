"""Stage extraction: the first reading of every map (evidence V1)."""
import copy
from ..engine import check_map_dates, evidence_context, evidence_images, evidence_inputs, parse_reply, stage_request
from ..prompts import evidence_prompt
from ..science import evidence_contract as ec

STAGE = 'extraction'


def request(state):
    case, previous, lookup = evidence_inputs(STAGE, state)
    payload = dict(case=case)
    if previous:
        payload['initial_extraction'] = ec.model_evidence(previous, lookup)
    system, effective = evidence_prompt(state, STAGE)
    return stage_request(STAGE, system, payload, evidence_images(state, lookup), ec.schema(STAGE, lookup), ec.VERSION,
                         effective)


def accept(state, response, namespace):
    value = parse_reply(response)
    result = copy.deepcopy(state)
    previous, lookup = evidence_context(STAGE, state)
    value = ec.schema(STAGE, lookup).model_validate(value).model_dump()
    evidence, allocations = ec.normalize_evidence(value, state['pack'], lookup, previous, namespace)
    check_map_dates(evidence, state)
    result['evidence_v1'] = evidence
    result['allocations'] = allocations
    return result
