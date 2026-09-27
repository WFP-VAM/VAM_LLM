"""Stage feedback: the evidence updated from the analyst's comments, with a resolution for each comment."""
import copy
from ..engine import check_map_dates, evidence_context, evidence_images, evidence_inputs, parse_reply, stage_request
from ..prompts import evidence_prompt
from ..science import evidence_contract as ec

STAGE = 'feedback'


def request(state):
    case, previous, lookup = evidence_inputs(STAGE, state)
    payload = dict(case=case)
    if previous:
        payload['latest_extraction'] = ec.model_evidence(previous, lookup)
    payload['analyst_comments'] = state['analyst_comments']
    system, effective = evidence_prompt(state, STAGE)
    return stage_request(STAGE, system, payload, evidence_images(state, lookup), ec.schema(STAGE, lookup), ec.VERSION,
                         effective)


def accept(state, response, namespace):
    value = parse_reply(response)
    result = copy.deepcopy(state)
    previous, lookup = evidence_context(STAGE, state)
    value = ec.schema(STAGE, lookup).model_validate(value).model_dump()
    evidence, allocations = ec.normalize_evidence(value['evidence'], state['pack'], lookup, previous, namespace)
    check_map_dates(evidence, state)
    for r in value['resolutions']:
        if r['analyst_quote'] not in state['analyst_comments']:
            raise ValueError('Feedback quote is not verbatim')
    result['feedback_resolutions'] = [{**r, 'evidence_ids': [lookup['evidence'][e] for e in r['evidence_ids']]} for r in value['resolutions']]
    result['evidence'] = evidence
    result['allocations'] = allocations
    return result
