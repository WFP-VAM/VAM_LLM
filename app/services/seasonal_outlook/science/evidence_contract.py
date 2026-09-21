"""Operational vision contracts. Python owns identifiers; scientific fields stay intact."""
import copy
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, create_model
from .schemas import Record, MapReading, EvidenceBundle
from .files import validate_evidence
from .refinement_schemas import VisualReview, validate_review as validate_legacy_review

VERSION = 'seasonal_evidence_v1'
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SignalData(Record):
    geography: Text
    signal: Text
    magnitude_as_read: str | None
    legend_class: str | None
    location_in_figure: Text
    confidence: Literal['high', 'medium', 'low']
    limitations: list[Text]


class Problem(Record):
    kind: Literal['error', 'omission', 'uncertainty']
    evidence_ids: list[str]
    field: Text
    location_in_figure: Text
    visual_basis: Text
    proposed_change: Text
    confidence: Literal['high', 'medium', 'low']


class MapReview(Record):
    figure_id: str
    coverage_summary: Text
    issues: list[Problem]
    limitations: list[Text]


class Resolution(Record):
    issue_id: str
    decision: Literal['corrected', 'declined', 'unresolved']
    visual_basis: Text


class FeedbackResolution(Record):
    analyst_quote: Text
    decision: Literal['applied', 'partially_applied', 'not_applied', 'unverifiable']
    explanation: Text
    evidence_ids: list[str]


def aliases(pack, previous=None, review=None):
    maps = {f'f{i:02}': fid for i, fid in enumerate(pack['figure_ids'], 1)}
    ids = [s['evidence_id'] for m in (previous or {}).get('maps', []) for s in m['signals']]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate source evidence IDs.')
    issues = [i['issue_id'] for m in (review or {}).get('maps', []) for i in m['issues']]
    if len(issues) != len(set(issues)):
        raise ValueError('Duplicate source issue IDs.')
    return dict(figures=maps, evidence={f'e{i:03}': eid for i, eid in enumerate(ids, 1)},
                issues={f'r{i:03}': iid for i, iid in enumerate(issues, 1)})


def allowed(values):
    return Literal[tuple(values)] if values else str


def ref_list(values):
    return (list[allowed(values)], Field(max_length=0) if not values else ...)


def schema(stage, lookup):
    eid, fid, iid = lookup['evidence'], lookup['figures'], lookup['issues']
    signal = SignalData if stage == 'extraction' else create_model('RevisedSignal', __base__=SignalData,
        source_id=(allowed(eid) | None if eid else type(None), ...))
    reading = create_model('MapEvidence', __base__=MapReading,
        figure_id=(allowed(fid), ...), signals=(list[signal], ...))
    bundle = create_model('MapEvidenceBundle', __base__=Record, maps=(list[reading], Field(min_length=1)))
    if stage == 'extraction':
        return create_model('VisionExtraction', __base__=bundle)
    if stage == 'review':
        problem = create_model('LocatedProblem', __base__=Problem, evidence_ids=ref_list(eid))
        reviewed = create_model('ReviewedMap', __base__=MapReview,
            figure_id=(allowed(fid), ...), issues=(list[problem], ...))
        return create_model('VisionReview', __base__=Record,
            maps=(list[reviewed], Field(min_length=1)), overall_limitations=(list[Text], ...))
    if stage == 'refinement':
        resolution = create_model('IssueDecision', __base__=Resolution, issue_id=(allowed(iid), ...))
        return create_model('VisionRefinement', __base__=Record, evidence=(bundle, ...),
            issue_resolutions=(list[resolution], Field(max_length=0) if not iid else ...), limitations=(list[Text], ...))
    resolution = create_model('CommentDecision', __base__=FeedbackResolution, evidence_ids=ref_list(eid))
    return create_model('VisionFeedback', __base__=Record, evidence=(bundle, ...),
        resolutions=(list[resolution], Field(min_length=1)), limitations=(list[Text], ...))


def model_evidence(bundle, lookup):
    figures = {v: k for k, v in lookup['figures'].items()}
    ids = {v: k for k, v in lookup['evidence'].items()}
    result = copy.deepcopy(bundle)
    result.pop('case_id', None)
    for m in result['maps']:
        m['figure_id'] = figures[m['figure_id']]
        for s in m['signals']:
            s['source_id'] = ids[s.pop('evidence_id')]
    return result


def model_review(review, lookup):
    figures, eids, iids = [{v: k for k, v in lookup[kind].items()} for kind in ('figures', 'evidence', 'issues')]
    return dict(maps=[dict(figure_id=figures[m['figure_id']],
        coverage_summary=m.get('coverage_summary') or '\n'.join(
            c['area_in_figure']+': '+c['visual_signal_or_limit'] for c in m.get('coverage_scan', [])),
        issues=[{**i, 'issue_id': iids[i['issue_id']], 'evidence_ids': [eids[e] for e in i['evidence_ids']]}
                for i in m['issues']], limitations=m['limitations']) for m in review['maps']],
        overall_limitations=review['overall_limitations'])


def check_inventory(maps, lookup):
    ids = [m['figure_id'] for m in maps]
    if len(ids) != len(set(ids)) or set(ids) != set(lookup['figures']):
        raise ValueError('figure_inventory_mismatch')


def normalize_evidence(value, pack, lookup, previous, namespace):
    check_inventory(value['maps'], lookup)
    old = {s['evidence_id']: m['figure_id'] for m in (previous or {}).get('maps', []) for s in m['signals']}
    used = set()
    result = copy.deepcopy(value)
    allocations = []
    for m in result['maps']:
        m['figure_id'] = lookup['figures'][m['figure_id']]
        for n, s in enumerate(m['signals'], 1):
            source = s.pop('source_id', None)
            if source is not None:
                if source not in lookup['evidence']:
                    raise ValueError('unknown_source_evidence')
                eid = lookup['evidence'][source]
                if old[eid] != m['figure_id']:
                    raise ValueError('cross_map_source_evidence')
            else:
                eid = f'{m["figure_id"]}__{namespace}__s{n:03}'
                if eid in old:
                    raise ValueError('new_evidence_id_collision')
            if eid in used:
                raise ValueError('duplicate_source_evidence')
            used.add(eid)
            s['evidence_id'] = eid
            allocations.append(dict(figure_id=m['figure_id'], position=n, source_id=source, evidence_id=eid))
        if not m['signals'] and not any(x.strip() for x in m['limitations']):
            raise ValueError('empty_map_without_explanation')
    result['case_id'] = pack['case_id']
    errors = validate_evidence(EvidenceBundle.model_validate(result), pack)
    if errors:
        raise ValueError(', '.join(errors))
    return result, allocations


def normalize_review(value, pack, lookup, previous):
    check_inventory(value['maps'], lookup)
    owner = {s['evidence_id']: m['figure_id'] for m in previous['maps'] for s in m['signals']}
    result = copy.deepcopy(value)
    for m in result['maps']:
        m['figure_id'] = lookup['figures'][m['figure_id']]
        for n, issue in enumerate(m['issues'], 1):
            issue['evidence_ids'] = [lookup['evidence'][e] for e in issue['evidence_ids']]
            if any(owner[e] != m['figure_id'] for e in issue['evidence_ids']):
                raise ValueError('cross_map_review_reference')
            issue['issue_id'] = f'{m["figure_id"]}__r{n:03}'
    return dict(format_version=VERSION, case_id=pack['case_id'], **result)


def validate_saved_review(review, previous, pack):
    if review.get('format_version') != VERSION:
        if validate_legacy_review(VisualReview.model_validate(review), previous, pack):
            raise ValueError('Invalid saved legacy visual review.')
        return
    lookup = aliases(pack, previous, review)
    wire = model_review(review, lookup)
    for m in wire['maps']:
        for issue in m['issues']:
            issue.pop('issue_id')
    parsed = schema('review', lookup).model_validate(wire).model_dump()
    if normalize_review(parsed, pack, lookup, previous) != review:
        raise ValueError('Invalid saved visual review.')


def recover_prefix_only(evidence, pack):
    """Recover this single historical defect, never guess figures or scientific content."""
    parsed = EvidenceBundle.model_validate(evidence)
    if validate_evidence(parsed, pack) != ['evidence_id_wrong_prefix']:
        raise ValueError('Only an isolated evidence ID prefix failure can be normalized.')
    result = copy.deepcopy(evidence)
    mapping = []
    for m in result['maps']:
        for n, s in enumerate(m['signals'], 1):
            original = s['evidence_id']
            s['evidence_id'] = f'{m["figure_id"]}__recovered__s{n:03}'
            mapping.append(dict(original_id=original, evidence_id=s['evidence_id'], figure_id=m['figure_id']))
    if validate_evidence(EvidenceBundle.model_validate(result), pack):
        raise ValueError('Recovered evidence failed validation.')
    return result, mapping
