"""Stage review: a visual review of the extraction against every original map."""
import copy
from ..engine import evidence_context, evidence_images, evidence_inputs, parse_reply, stage_request
from ..prompts import evidence_prompt
from ..science import evidence_contract as ec

STAGE = 'review'


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
    result['review'] = ec.normalize_review(value, state['pack'], lookup, previous)
    return result
