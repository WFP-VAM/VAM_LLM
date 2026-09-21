"""Small operational GLM contracts; identifiers and provenance belong to Python."""
import copy
from typing import Annotated, Literal
from pydantic import Field, StringConstraints, create_model, model_validator
from .schemas import Record

VERSION = 'seasonal_report_v1'
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Paragraph(Record):
    text: Text
    evidence_ids: list[str] = Field(min_length=1)
    season_ids: list[str] = Field(min_length=1)


class Report(Record):
    headline: Text
    headline_evidence_ids: list[str] = Field(min_length=1)
    paragraphs: list[Paragraph] = Field(min_length=1)
    limitations: list[Text]


class DraftAnswer(Record):
    report: Report | None
    inability_reason: Text | None

    @model_validator(mode='after')
    def explicit_outcome(self):
        if (self.report is None) == (self.inability_reason is None):
            raise ValueError('Return either a complete report or an explicit inability reason.')
        return self


class Issue(Record):
    target_ids: list[str] = Field(min_length=1)
    evidence_ids: list[str]
    severity: Literal['minor', 'major', 'critical']
    problem: Text
    proposed_change: Text


class Review(Record):
    issues: list[Issue]
    limitations: list[Text]


def aliases(evidence):
    ids = [s['evidence_id'] for m in evidence['maps'] for s in m['signals']]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('Evidence identifiers must be nonempty and unique.')
    return {f'e{i:03}': eid for i, eid in enumerate(ids, 1)}


def model_evidence(evidence):
    lookup = {v: k for k, v in aliases(evidence).items()}
    result = copy.deepcopy(evidence)
    for m in result['maps']:
        for s in m['signals']:
            s['evidence_id'] = lookup[s['evidence_id']]
    return result


def model_report(report, evidence):
    lookup = {v: k for k, v in aliases(evidence).items()}
    return dict(headline_id='h1', headline=report['headline'],
        headline_evidence_ids=[lookup[e] for e in report['headline_evidence_ids']],
        paragraphs=[dict(block_id=p['block_id'], text=p['text'], season_ids=p['season_ids'],
                         evidence_ids=[lookup[e] for e in p['evidence_ids']]) for p in report['paragraphs']],
        limitations=report['limitations'])


def model_review(review, evidence):
    lookup = {v: k for k, v in aliases(evidence).items()}
    return dict(issues=[dict(issue_id=i['issue_id'], target_ids=i['target_ids'], severity=i['severity'],
        evidence_ids=[lookup[e] for e in i['evidence_ids']], problem=i['problem'],
        proposed_change=i['proposed_change']) for i in review['issues']], limitations=review['limitations'])


def draft_schema(state):
    eids = tuple(aliases(state['evidence']))
    sids = tuple(s['season_id'] for s in state['pack']['calendar'] if s['mode'] != 'excluded')
    if not sids:
        raise ValueError('No eligible seasonal cycle.')
    paragraph = create_model('SourcedParagraph', __base__=Paragraph,
        evidence_ids=(list[Literal[eids]], Field(min_length=1)),
        season_ids=(list[Literal[sids]], Field(min_length=1)))
    report = create_model('SourcedReport', __base__=Report,
        headline_evidence_ids=(list[Literal[eids]], Field(min_length=1)),
        paragraphs=(list[paragraph], Field(min_length=1)))
    return create_model('ReportAnswer', __base__=DraftAnswer, report=(report | None, ...))


def review_schema(state):
    targets = ('report', 'h1', *(p['block_id'] for p in state['initial_analysis']['paragraphs']))
    issue = create_model('SourcedIssue', __base__=Issue,
        target_ids=(list[Literal[targets]], Field(min_length=1)),
        evidence_ids=(list[Literal[tuple(aliases(state['evidence']))]], ...))
    return create_model('ProblemReview', __base__=Review, issues=(list[issue], ...))


def normalize_report(report, state):
    lookup = aliases(state['evidence'])
    return dict(format_version=VERSION, case_id=state['pack']['case_id'], headline=report.headline,
        headline_evidence_ids=[lookup[e] for e in report.headline_evidence_ids],
        paragraphs=[dict(block_id=f'p{i}', text=p.text, evidence_ids=[lookup[e] for e in p.evidence_ids],
                         season_ids=p.season_ids) for i, p in enumerate(report.paragraphs, 1)],
        limitations=report.limitations)


def normalize_review(review, state):
    lookup = aliases(state['evidence'])
    return dict(format_version=VERSION, case_id=state['pack']['case_id'],
        issues=[dict(issue_id=f'r{i:03}', **{**item.model_dump(),
            'evidence_ids': [lookup[e] for e in item.evidence_ids]}) for i, item in enumerate(review.issues, 1)],
        limitations=review.limitations)


def validate_report(report, state):
    """Mechanical bounds only; prose/source fidelity still requires scientific review."""
    errors = []
    signals = {s['evidence_id']: m for m in state['evidence']['maps'] for s in m['signals']}
    modes = {s['season_id']: s['mode'] for s in state['pack']['calendar']}
    if report.get('case_id') != state['pack']['case_id']:
        errors.append('case_id_mismatch')
    if not report.get('headline', '').strip() or not report.get('paragraphs'):
        errors.append('empty_report')
    linked = set()
    for p in report.get('paragraphs', []):
        if not p['text'].strip() or not p['evidence_ids'] or not p['season_ids']:
            errors.append('empty_or_unlinked_paragraph')
        for eid in p['evidence_ids']:
            linked.add(eid)
            m = signals.get(eid)
            if m is None:
                errors.append('unknown_evidence:' + eid)
                continue
            if m['readability'] == 'unreadable':
                errors.append('unreadable_evidence:' + eid)
            for sid in p['season_ids']:
                mode = modes.get(sid)
                if mode is None or mode == 'excluded':
                    errors.append('ineligible_season:' + sid)
                if mode == 'past_only' and m['product_kind'] != 'observed':
                    errors.append('postseason_non_observed:' + eid)
                if mode == 'future_only' and m['product_kind'] not in ('forecast', 'mixed'):
                    errors.append('preseason_non_future:' + eid)
    refs = set(report.get('headline_evidence_ids', []))
    if not refs or not refs <= linked:
        errors.append('headline_sources_not_in_body')
    return sorted(set(errors))


def validate_review(review, state):
    targets = {'report', 'h1', *(p['block_id'] for p in state['initial_analysis']['paragraphs'])}
    refs = set(aliases(state['evidence']).values())
    errors = []
    for issue in review['issues']:
        if not set(issue['target_ids']) <= targets or not set(issue['evidence_ids']) <= refs:
            errors.append('unknown_review_reference')
        if not issue['target_ids'] or not issue['problem'].strip() or not issue['proposed_change'].strip():
            errors.append('empty_review_issue')
    return errors
