"""Pure stage preparation/validation, separated from durable execution: the phases' stage chains, an analysis's
initial state, and what the stage modules in nodes/ share."""
import json
from datetime import date
from .science import evidence_contract as ec
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


def evidence_inputs(stage, state):
    """What an evidence stage's request starts from: the case under short aliases, the prior evidence, the aliases."""
    case = model_case(state['pack'])
    previous, lookup = evidence_context(stage, state)
    case.pop('case_id')
    case['figure_ids'] = list(lookup['figures'])
    return case, previous, lookup


def evidence_images(state, lookup):
    """The maps an evidence stage sends, each under its short alias."""
    reverse = {v: k for k, v in lookup['figures'].items()}
    return [{**m, 'figure_id': reverse[m['figure_id']]} for m in state['images']]


def stage_request(stage, system, payload, images, schema, version, effective):
    """A stage's request as the analysis record keeps it."""
    return dict(stage=stage, system=system, payload=payload, images=images, schema=schema.model_json_schema(),
                contract_version=version, prompt_version='seasonal-prompts-v1', effective_rules=effective)


def parse_reply(response):
    """The reply's JSON value, refused when the reply is blocked, incomplete or empty."""
    if response.get('finish_reason') not in ('STOP', 'stop'):
        raise ValueError('Model response blocked or incomplete: ' + str(response.get('finish_reason')))
    if not response.get('text', '').strip():
        raise ValueError('Empty model response')
    return json.loads(response['text'])


def check_map_dates(evidence, state):
    """Refuse evidence with a map issued after the input cutoff or valid over a reversed interval."""
    cutoff = date.fromisoformat(state['pack']['report_date'])
    for m in evidence['maps']:
        if m.get('issue_date') and date.fromisoformat(m['issue_date']) > cutoff:
            raise ValueError('Map issue date is after the input cutoff')
        if m.get('valid_start') and m.get('valid_end') and date.fromisoformat(m['valid_start']) > date.fromisoformat(m['valid_end']):
            raise ValueError('Map validity interval is reversed')
