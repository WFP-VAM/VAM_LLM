"""Stage report_review: a review of the first report against the evidence and calendar (draft review)."""
import copy
from ..engine import parse_reply, stage_request
from ..prompts import effective_rules, report_prompt
from ..science import report_contract as rc
from ..science.files import model_case

STAGE = 'report_review'


def request(state):
    case = model_case(state['pack'])
    # Explicit allowlist: no images, raw analyst comments or unconfirmed candidates.
    payload = dict(case=case, evidence=rc.model_evidence(state['evidence']))
    payload['initial_analysis'] = rc.model_report(state['initial_analysis'], state['evidence'])
    system, effective, images = report_prompt(STAGE, state), effective_rules(state), []
    return stage_request(STAGE, system, payload, images, rc.review_schema(state), rc.VERSION, effective)


def accept(state, response, namespace):
    value = parse_reply(response)
    result = copy.deepcopy(state)
    parsed = rc.review_schema(state).model_validate(value)
    report = rc.normalize_review(parsed, state)
    errors = rc.validate_review(report, state)
    result['draft_review'] = report
    if errors:
        raise ValueError(', '.join(errors))
    return result
