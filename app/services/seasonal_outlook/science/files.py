def model_case(pack):
    # Same allowlist as the previous benchmark. No paths, original prose or evaluation files.
    return {k: pack[k] for k in ('case_id', 'report_month', 'region_label', 'calendar', 'figure_ids', 'availability_note', 'limitations')}

def validate_evidence(evidence, pack):
    errors = []
    if evidence.case_id != pack['case_id']:
        errors.append('case_id_mismatch')
    ids = [m.figure_id for m in evidence.maps]
    if len(ids) != len(set(ids)) or set(ids) != set(pack['figure_ids']):
        errors.append('figure_inventory_mismatch')
    seen = set()
    for m in evidence.maps:
        if m.readability == 'unreadable' and m.signals:
            errors.append('unreadable_map_has_signals')
        for signal in m.signals:
            if signal.evidence_id in seen:
                errors.append('duplicate_evidence_id')
            if not signal.evidence_id.startswith(m.figure_id):
                errors.append('evidence_id_wrong_prefix')
            seen.add(signal.evidence_id)
    return sorted(set(errors))
