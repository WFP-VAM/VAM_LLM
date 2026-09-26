"""Faithful prototype presentation, with provider-neutral labels."""
from datetime import datetime, timezone

import streamlit as st

from app.shared.llm.errors import CONTRACT_FAILURES

from .calls import operation_name


def evidence_panel(reading):
    st.subheader(reading['title_as_read'] or 'Map evidence')
    st.write('Product: ' + shown(reading['product_kind']) + ' · ' + shown(reading['variable']))
    st.write('Period: ' + shown(reading['period_as_read']))
    st.write('Readability: ' + shown(reading['readability']))
    with st.expander('Map information and complete legend'):
        for field in ('metric', 'units', 'reference_baseline', 'issue_date', 'valid_start', 'valid_end', 'geographic_coverage', 'legend_as_read'):
            st.write(field.replace('_', ' ').capitalize() + ': ' + shown(reading[field]))
    for limitation in reading['limitations']:
        st.warning(limitation)
    st.caption('Confidence is reported by the model, not an independent accuracy score.')
    if not reading['signals']:
        st.info('No readable signals extracted. Check the map limitations.')
    with st.container(height=550, border=False):
        for signal in reading['signals']:
            with st.container(border=True):
                st.markdown('**' + signal['geography'] + '**')
                st.write(signal['signal'])
                for label, field in (('Magnitude', 'magnitude_as_read'), ('Legend class', 'legend_class'),
                                     ('Location in map', 'location_in_figure'), ('Confidence', 'confidence')):
                    st.write(label + ': ' + shown(signal[field]))
                for limitation in signal['limitations']:
                    st.write('Limitation: ' + limitation)
                st.caption(signal['evidence_id'])

def shown(value):
    return str(value) if value is not None and str(value).strip() else 'Not established'

def differences(record):
    changes = record.get('differences', [])
    st.caption(f'{len(changes)} recorded differences from the parent version. A change is not automatically an improvement.')
    for i, item in enumerate(changes, 1):
        with st.expander(f'{i}. {item.get("evidence_id") or item.get("field")} · {item.get("action", "modified")}'):
            a, b = st.columns(2)
            for col, label, content in [(a, 'Before', item.get('before')), (b, 'After', item.get('after'))]:
                with col:
                    st.markdown('**'+label+'**')
                    if isinstance(content, dict):
                        for name, value in content.items():
                            st.write(name.replace('_', ' ').capitalize()+': '+shown(value))
                    else:
                        st.write(shown(content))

def analysis_view(analysis):
    st.subheader(analysis['headline'])
    for p in analysis['paragraphs']:
        st.write(p['text'])
    if analysis.get('format_version') == 'seasonal_report_v1':
        words = len((' '.join([analysis['headline'], *[p['text'] for p in analysis['paragraphs']]])).split())
        st.caption(f'{words} words · Direct evidence references · Scientific review remains required.')
        if words > 350:
            st.info('This draft exceeds the approximate 350-word editorial target.')
        with st.expander('Sources for headline and paragraphs'):
            st.write('Headline: '+', '.join(analysis['headline_evidence_ids']))
            for p in analysis['paragraphs']:
                st.write(p['block_id']+' · Cycles: '+', '.join(p['season_ids']))
                st.write('Sources: '+', '.join(p['evidence_ids']))
    for field in ('missing_context', 'limitations', 'intentional_omissions'):
        if analysis.get(field):
            with st.expander(field.replace('_', ' ').capitalize()):
                for item in analysis[field]:
                    st.write('• '+item)

def llm_diagnostics(operation):
    """The operation's model calls in the shape of the shared LLM diagnostics panel.

    Each call entry is one attempt. A failed attempt that a later attempt of the same stage made good counts as
    recovered, as the shared tracer counts it.
    """
    calls = operation.get('calls') or []
    records = []
    for index, call in enumerate(calls):
        status = {'validated': 'succeeded', 'failed': 'failed'}.get(call.get('status'), 'started')
        if status == 'failed' and any(later.get('stage') == call.get('stage') and later.get('status') == 'validated'
                                      for later in calls[index + 1:]):
            status = 'recovered'
        started = call.get('started_at')
        if isinstance(started, (int, float)):  # the record's clock until the reply's trace time replaces it
            started = datetime.fromtimestamp(started, timezone.utc).isoformat()
        duration = call.get('duration_seconds')
        records.append(dict(call_id=call.get('call_id') or f'call {index + 1}', node=call.get('stage'),
                            operation=operation_name(call.get('stage')), status=status, started_at=started,
                            duration_ms=round(duration * 1000) if isinstance(duration, (int, float)) else None,
                            model=call.get('model'), failure_code=call.get('failure_code'),
                            disposition='recovered_by_retry' if status == 'recovered' else None))
    active = [r for r in records if r['status'] == 'started']
    failed = [r for r in records if r['status'] == 'failed']
    return dict(calls=records, current_call_id=active[-1]['call_id'] if active else None,
                succeeded_calls=sum(r['status'] in ('succeeded', 'recovered') for r in records),
                recovered_calls=sum(r['status'] == 'recovered' for r in records), failed_calls=len(failed),
                contract_failed_calls=sum(r['failure_code'] in CONTRACT_FAILURES for r in failed))


def review_view(review):
    if review.get('summary'):
        st.write(review['summary'])
    for item in review.get('resolutions', review.get('issue_resolutions', [])):
        with st.container(border=True):
            st.write(item.get('analyst_quote', item.get('issue_id', 'Resolution')))
            st.write('Decision: '+item.get('decision', ''))
            st.write(item.get('explanation', item.get('visual_basis', item.get('rationale', ''))))
    for m in review.get('maps', []):
        with st.expander(m['figure_id']):
            if m.get('coverage_summary'):
                st.write('**Map coverage and reading limits**')
                st.write(m['coverage_summary'])
            for issue in m.get('issues', []):
                st.write('**'+issue['kind'].capitalize()+'** · '+issue.get('location_in_figure', ''))
                st.write(issue.get('visual_basis', ''))
                st.write('Proposed change: '+issue.get('proposed_change', ''))
            if not m.get('issues'):
                st.write('No issues flagged by the automatic reviewer.')
            for limit in m.get('limitations', []):
                st.warning(limit)
    if review.get('format_version') == 'seasonal_evidence_v1':
        st.caption('The reviewer lists problems and uncertainties, not individual confirmations. Scientific review remains required.')
        for limit in review.get('overall_limitations', []):
            st.write('Review limitation: '+limit)
    for issue in review.get('issues', []):
        with st.container(border=True):
            st.write(issue.get('severity', '').capitalize()+' · '+issue.get('kind', ''))
            if issue.get('target_ids'):
                st.caption('Applies to: '+', '.join(issue['target_ids']))
            st.write(issue.get('problem', issue.get('rationale', '')))
            st.write(issue.get('proposed_change', ''))
    if review.get('format_version') == 'seasonal_report_v1':
        if not review.get('issues'):
            st.info('The reviewer reported no actionable problems. This does not certify scientific correctness.')
        for limit in review.get('limitations', []):
            st.write('Review limitation: '+limit)
    with st.expander('Complete review record'):
        st.json(review)
