"""Stage redraft: the complete revised report, after the review."""
import copy
from ..engine import parse_reply, stage_request
from ..prompts import effective_rules, report_prompt
from ..science import report_contract as rc
from ..science.files import model_case

STAGE = 'redraft'


def request(state):
    case = model_case(state['pack'])
    # Explicit allowlist: no images, raw analyst comments or unconfirmed candidates.
    payload = dict(case=case, evidence=rc.model_evidence(state['evidence']))
    payload['initial_analysis'] = rc.model_report(state['initial_analysis'], state['evidence'])
    payload['draft_review'] = rc.model_review(state['draft_review'], state['evidence'])
    system, effective, images = report_prompt(STAGE, state), effective_rules(state), []
    return stage_request(STAGE, system, payload, images, rc.draft_schema(state), rc.VERSION, effective)


def accept(state, response, namespace):
    value = parse_reply(response)
    result = copy.deepcopy(state)
    parsed = rc.draft_schema(state).model_validate(value)
    if parsed.report is None:
        raise ValueError('Insufficient evidence: ' + parsed.inability_reason)
    report = rc.normalize_report(parsed.report, state)
    errors = rc.validate_report(report, state)
    result['report'] = report
    if errors:
        raise ValueError(', '.join(errors))
    return result
