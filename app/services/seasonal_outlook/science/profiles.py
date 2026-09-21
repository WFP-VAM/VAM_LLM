import json
from pathlib import Path
from .schemas import StudyRule

RESOURCES = Path(__file__).parents[1] / 'resources'


def profiles():
    def read(name):
        return json.loads((RESOURCES / name).read_text(encoding='utf-8'))
    base, evidence, report = [read(n + '_rules.json') for n in ('base', 'evidence', 'report')]
    for group in (base, evidence, report):
        group['rules'] = [StudyRule.model_validate(r).model_dump() for r in group['rules']]
        ids = [r['rule_id'] for r in group['rules']]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate scientific rule')
    if set(evidence['stages']) != {'extraction', 'review', 'refinement'}:
        raise ValueError('Invalid evidence stage inventory')
    for ids in evidence['stages'].values():
        if not set(ids) <= {r['rule_id'] for r in evidence['rules']}:
            raise ValueError('Unknown scientific rule')
    return dict(arm='rules', rules=base['rules'], evidence_profile=evidence, report_profile=report)


def stage_state(state, stage):
    profile = state['evidence_profile']
    additions = [r for r in profile['rules'] if r['rule_id'] in profile['stages'][stage]]
    return {**state, 'rules': state['rules'] + additions}
