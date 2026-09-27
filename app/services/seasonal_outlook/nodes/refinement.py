"""Stage refinement: the complete revised extraction (evidence), with a decision on each review issue."""
import copy
from ..engine import check_map_dates, evidence_context, evidence_images, evidence_inputs, parse_reply, stage_request
from ..prompts import evidence_prompt
from ..science import evidence_contract as ec

STAGE = 'refinement'


def request(state):
    case, previous, lookup = evidence_inputs(STAGE, state)
    payload = dict(case=case)
    if previous:
        payload['initial_extraction'] = ec.model_evidence(previous, lookup)
    payload['visual_review'] = ec.model_review(state['review'], lookup)
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
    ids = [r['issue_id'] for r in value['issue_resolutions']]
    if len(ids) != len(set(ids)) or set(ids) != set(lookup['issues']):
        raise ValueError('Incomplete or duplicate visual issue decisions')
    result['issue_resolutions'] = [{**r, 'issue_id': lookup['issues'][r['issue_id']]} for r in value['issue_resolutions']]
    result['evidence'] = evidence
    result['allocations'] = allocations
    return result
