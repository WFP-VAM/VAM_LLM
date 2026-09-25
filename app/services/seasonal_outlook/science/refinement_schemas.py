"""Contracts for one visual review and one revision of an existing extraction."""
from typing import Literal
from pydantic import Field
from .schemas import Record, EvidenceBundle
from .files import validate_evidence

METADATA_FIELDS = ('title_as_read', 'product_kind', 'variable', 'metric', 'units',
    'reference_baseline', 'period_as_read', 'valid_start', 'valid_end', 'issue_date',
    'geographic_coverage', 'legend_as_read', 'readability')


class MetadataCheck(Record):
    field: Literal['title_as_read', 'product_kind', 'variable', 'metric', 'units',
        'reference_baseline', 'period_as_read', 'valid_start', 'valid_end', 'issue_date',
        'geographic_coverage', 'legend_as_read', 'readability']
    verdict: Literal['confirmed', 'disputed', 'unverifiable']
    visual_basis: str
    issue_ids: list[str]


class SignalCheck(Record):
    evidence_id: str
    verdict: Literal['confirmed', 'disputed', 'unverifiable']
    visual_basis: str
    issue_ids: list[str]


class CoverageCheck(Record):
    area_in_figure: str
    visual_signal_or_limit: str
    covered_by_evidence_ids: list[str]
    issue_ids: list[str]


class ReviewIssue(Record):
    issue_id: str
    kind: Literal['error', 'omission', 'uncertainty']
    evidence_ids: list[str]
    field: str
    location_in_figure: str
    visual_basis: str
    proposed_change: str
    confidence: Literal['high', 'medium', 'low']


class MapReview(Record):
    figure_id: str
    metadata_checks: list[MetadataCheck]
    signal_checks: list[SignalCheck]
    coverage_scan: list[CoverageCheck] = Field(min_length=1)
    issues: list[ReviewIssue]
    limitations: list[str]


class VisualReview(Record):
    case_id: str
    maps: list[MapReview]
    overall_limitations: list[str]


class IssueResolution(Record):
    issue_id: str
    decision: Literal['corrected', 'declined', 'unresolved']
    visual_basis: str


class RefinedExtraction(Record):
    evidence: EvidenceBundle
    issue_resolutions: list[IssueResolution]
    limitations: list[str]


def validate_review(review, v1, pack):
    errors = []
    originals = {m['figure_id']: m for m in v1['maps']}
    ids = [m.figure_id for m in review.maps]
    if review.case_id != pack['case_id'] or set(ids) != set(originals) or len(ids) != len(set(ids)):
        return ['review_inventory_mismatch']
    all_issues = set()
    for m in review.maps:
        source_ids = {s['evidence_id'] for s in originals[m.figure_id]['signals']}
        checks = [s.evidence_id for s in m.signal_checks]
        if set(checks) != source_ids or len(checks) != len(source_ids):
            errors.append('signal_check_inventory:' + m.figure_id)
        fields = [c.field for c in m.metadata_checks]
        if set(fields) != set(METADATA_FIELDS) or len(fields) != len(METADATA_FIELDS):
            errors.append('metadata_check_inventory:' + m.figure_id)
        issue_ids = [i.issue_id for i in m.issues]
        if len(issue_ids) != len(set(issue_ids)) or all_issues.intersection(issue_ids):
            errors.append('duplicate_review_issue')
        all_issues.update(issue_ids)
        for issue in m.issues:
            if not issue.issue_id.startswith(m.figure_id) or not set(issue.evidence_ids).issubset(source_ids):
                errors.append('invalid_issue_reference:' + issue.issue_id)
        for c in [*m.metadata_checks, *m.signal_checks, *m.coverage_scan]:
            if not set(c.issue_ids).issubset(issue_ids):
                errors.append('unknown_review_issue:' + m.figure_id)
            if getattr(c, 'verdict', None) == 'disputed' and not c.issue_ids:
                errors.append('dispute_without_issue:' + m.figure_id)
        for c in m.coverage_scan:
            if not set(c.covered_by_evidence_ids).issubset(source_ids):
                errors.append('coverage_unknown_evidence:' + m.figure_id)
    return sorted(set(errors))


def validate_refinement(refined, review, pack):
    errors = validate_evidence(refined.evidence, pack)
    required = {i['issue_id'] for m in review['maps'] for i in m['issues']}
    actual = [r.issue_id for r in refined.issue_resolutions]
    if set(actual) != required or len(actual) != len(required):
        errors.append('issue_resolution_inventory_mismatch')
    return errors


def evidence_diff(v1, v2):
    """Mechanical diff; never treats a model's assertion of improvement as a score."""
    changes = []
    before = {m['figure_id']: m for m in v1['maps']}
    for m in v2['maps']:
        old = before[m['figure_id']]
        for field in [*METADATA_FIELDS, 'limitations']:
            if old[field] != m[field]:
                changes.append(dict(figure_id=m['figure_id'], target='metadata', field=field,
                    before=old[field], after=m[field]))
        a = {s['evidence_id']: s for s in old['signals']}
        b = {s['evidence_id']: s for s in m['signals']}
        for eid in sorted(a.keys() | b.keys()):
            if a.get(eid) != b.get(eid):
                changes.append(dict(figure_id=m['figure_id'], target='signal', evidence_id=eid,
                    action='added' if eid not in a else 'removed' if eid not in b else 'modified',
                    before=a.get(eid), after=b.get(eid)))
    return changes
