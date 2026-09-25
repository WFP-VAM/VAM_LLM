"""Mechanical comparison of two evidence versions, shown to the analyst on page 5."""

METADATA_FIELDS = ('title_as_read', 'product_kind', 'variable', 'metric', 'units',
    'reference_baseline', 'period_as_read', 'valid_start', 'valid_end', 'issue_date',
    'geographic_coverage', 'legend_as_read', 'readability')


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
