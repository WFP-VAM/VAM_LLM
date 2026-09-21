"""Pure stage preparation/validation, separated from durable execution."""
import copy
import json
from datetime import date
from .science import evidence_contract as ec, report_contract as rc, evidence_prompts as ep, report_prompts as rp
from .science.files import model_case
from .science.profiles import profiles

EVIDENCE_STAGES = ('extraction', 'review', 'refinement', 'feedback')
CHAINS = {'extract': ['extraction', 'review', 'refinement'], 'feedback': ['feedback'],
          'report': ['draft', 'report_review', 'redraft']}


def initial_state(pack, maps):
    return dict(pack=pack, images=[dict(figure_id=f['figure_id'], metadata_note=f['metadata_note'],
                uri=m['object']['uri'], mime=m['object']['mime']) for f, m in zip(pack['figures'], maps)], **profiles())


def evidence_context(stage, state):
    previous = state.get('evidence') if stage == 'feedback' else state.get('evidence_v1')
    review = state.get('review') if stage == 'refinement' else None
    return previous, ec.aliases(state['pack'], previous, review)


def request_for(stage, state):
    case = model_case(state['pack'])
    if stage in EVIDENCE_STAGES:
        previous, lookup = evidence_context(stage, state)
        case.pop('case_id')
        case['figure_ids'] = list(lookup['figures'])
        payload = dict(case=case)
        if previous:
            payload['latest_extraction' if stage == 'feedback' else 'initial_extraction'] = ec.model_evidence(previous, lookup)
        if stage == 'refinement':
            payload['visual_review'] = ec.model_review(state['review'], lookup)
        if stage == 'feedback':
            payload['analyst_comments'] = state['analyst_comments']
        system, effective = ep.prompt(state, stage)
        reverse = {v: k for k, v in lookup['figures'].items()}
        images = [{**m, 'figure_id': reverse[m['figure_id']]} for m in state['images']]
        schema = ec.schema(stage, lookup)
        version = ec.VERSION
    else:
        # Explicit allowlist: no images, raw analyst comments or unconfirmed candidates.
        payload = dict(case=case, evidence=rc.model_evidence(state['evidence']))
        if stage != 'draft':
            payload['initial_analysis'] = rc.model_report(state['initial_analysis'], state['evidence'])
        if stage == 'redraft':
            payload['draft_review'] = rc.model_review(state['draft_review'], state['evidence'])
        system, effective, images = rp.prompt(stage, state), rp.effective_rules(state), []
        schema = rc.review_schema(state) if stage == 'report_review' else rc.draft_schema(state)
        version = rc.VERSION
    return dict(stage=stage, system=system, payload=payload, images=images, schema=schema.model_json_schema(),
                contract_version=version, prompt_version='seasonal-prompts-v1', effective_rules=effective)


def accept(stage, state, response, namespace):
    if response.get('finish_reason') not in ('STOP', 'stop'):
        raise ValueError('Model response blocked or incomplete: ' + str(response.get('finish_reason')))
    if not response.get('text', '').strip():
        raise ValueError('Empty model response')
    value = json.loads(response['text'])
    result = copy.deepcopy(state)
    if stage in EVIDENCE_STAGES:
        previous, lookup = evidence_context(stage, state)
        value = ec.schema(stage, lookup).model_validate(value).model_dump()
        if stage == 'review':
            result['review'] = ec.normalize_review(value, state['pack'], lookup, previous)
        else:
            evidence, allocations = ec.normalize_evidence(value if stage == 'extraction' else value['evidence'],
                state['pack'], lookup, previous, namespace)
            cutoff = date.fromisoformat(state['pack']['report_date'])
            for m in evidence['maps']:
                if m.get('issue_date') and date.fromisoformat(m['issue_date']) > cutoff:
                    raise ValueError('Map issue date is after the input cutoff')
                if m.get('valid_start') and m.get('valid_end') and date.fromisoformat(m['valid_start']) > date.fromisoformat(m['valid_end']):
                    raise ValueError('Map validity interval is reversed')
            if stage == 'refinement':
                ids = [r['issue_id'] for r in value['issue_resolutions']]
                if len(ids) != len(set(ids)) or set(ids) != set(lookup['issues']):
                    raise ValueError('Incomplete or duplicate visual issue decisions')
                result['issue_resolutions'] = [{**r, 'issue_id': lookup['issues'][r['issue_id']]} for r in value['issue_resolutions']]
            if stage == 'feedback':
                for r in value['resolutions']:
                    if r['analyst_quote'] not in state['analyst_comments']:
                        raise ValueError('Feedback quote is not verbatim')
                result['feedback_resolutions'] = [{**r, 'evidence_ids': [lookup['evidence'][e] for e in r['evidence_ids']]} for r in value['resolutions']]
            result['evidence_v1' if stage == 'extraction' else 'evidence'] = evidence
            result['allocations'] = allocations
    else:
        schema = rc.review_schema(state) if stage == 'report_review' else rc.draft_schema(state)
        parsed = schema.model_validate(value)
        if stage == 'report_review':
            report = rc.normalize_review(parsed, state)
            errors = rc.validate_review(report, state)
            result['draft_review'] = report
        else:
            if parsed.report is None:
                raise ValueError('Insufficient evidence: ' + parsed.inability_reason)
            report = rc.normalize_report(parsed.report, state)
            errors = rc.validate_report(report, state)
            result['initial_analysis' if stage == 'draft' else 'report'] = report
        if errors:
            raise ValueError(', '.join(errors))
    return result
