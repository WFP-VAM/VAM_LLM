"""Shared analysis history; inference starts only from explicit analyst actions."""
import hashlib
import json
import uuid
from datetime import date
import streamlit as st
from streamlit_shared import (apply_wfp_theme, render_wfp_sidebar_logo, render_onboarding_sidebar_button,
    render_instructions_sidebar_button, render_bug_report_sidebar_link, render_bug_report_header_link,
    request_json, request_bytes, safe_show_error)
from app.services.seasonal_outlook.inputs import calendar_for, checklist, region_option
from app.services.seasonal_outlook.ui import evidence_panel, differences, analysis_view, review_view
from app.services.seasonal_outlook.upload_ui import map_upload_panel
from app.services.seasonal_outlook.science.refinement_schemas import evidence_diff

st.set_page_config(page_title='Seasonal Outlook Drafter', layout='wide')
apply_wfp_theme()
st.markdown('''<style>
/* The shared WFP palette stays readable when Streamlit follows a dark OS theme. */
[data-testid="stExpander"] summary {background:var(--wfp-surface)!important;color:var(--wfp-text)!important;}
[data-testid="stExpander"] summary * {color:var(--wfp-text)!important;}
.stSelectbox [data-baseweb="select"] * {color:var(--wfp-text)!important;}
[data-baseweb="popover"] [role="listbox"], [role="option"] {background:var(--wfp-surface)!important;color:var(--wfp-text)!important;}
.bug-report-button {white-space:normal!important;max-width:100%;box-sizing:border-box;text-align:center;}
</style>''', unsafe_allow_html=True)
BASE = '/seasonal-outlook'
with st.sidebar:
    render_wfp_sidebar_logo()
    render_onboarding_sidebar_button(key='seasonal_onboarding')
    render_instructions_sidebar_button(key='seasonal_instructions')
    render_bug_report_sidebar_link()
title, help_link = st.columns([3, 1])
with title:
    st.title('Seasonal Outlook Drafter')
with help_link:
    render_bug_report_header_link()
st.caption('Upload climate maps, review the evidence, then confirm a version to draft a Seasonal Outlook report.')
info = request_json('GET', BASE + '/info')
enabled = info['enabled']
if not enabled:
    st.info('New processing is disabled. Existing analyses and downloads remain available when storage is configured.')
    if info['configuration_errors']:
        with st.expander('Service configuration'):
            st.write(info['configuration_errors'])


def remember(run):
    st.query_params['seasonal_run'] = run['id']
    st.session_state['seasonal_run'] = run['id']


def operation_id(run, action, fields):
    value = json.dumps([run['id'], run['revision'], action, fields], sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def act(run, action, **fields):
    body = dict(request_id=operation_id(run, action, fields), expected_revision=run['revision'], **fields)
    try:
        updated = request_json('POST', f'{BASE}/runs/{run["id"]}/{action}', json_body=body, timeout=90)
        remember(updated)
    except Exception as exc:
        safe_show_error(exc)
        return
    st.rerun()


with st.expander('New analysis', expanded=not st.query_params.get('seasonal_run')):
    viewing_run = bool(st.query_params.get('seasonal_run') or st.session_state.get('seasonal_run'))
    if viewing_run and st.button('Start another input package', disabled=not enabled):
        # Explicitly start a new creation intent, even with the same region/date.
        # Ordinary reruns retain the old key and remain idempotent.
        st.session_state.pop('seasonal_creation_seed', None)
        st.session_state.pop('seasonal_run', None)
        st.query_params.pop('seasonal_run', None)
        st.rerun()
    with st.form('seasonal_new'):
        region = st.selectbox('Region', info['regions'], index=None, format_func=region_option, placeholder='Select a region')
        cutoff = st.date_input('Report date / availability cutoff', value=date.today(), max_value=date.today())
        notes = st.text_area('Input notes', max_chars=10000)
        create = st.form_submit_button('Prepare input package', disabled=not enabled or viewing_run)
    if create:
        if region is None:
            st.error('Select a region explicitly.')
        else:
            seed = st.session_state.setdefault('seasonal_creation_seed', uuid.uuid4().hex)
            body = dict(region_id=region['region_id'], report_date=cutoff.isoformat(), notes=notes)
            body.update(request_id=hashlib.sha256((seed+json.dumps(body, sort_keys=True)).encode()).hexdigest(), expected_revision=0)
            try:
                remember(request_json('POST', BASE + '/runs', json_body=body))
                st.rerun()
            except Exception as exc:
                safe_show_error(exc)

with st.expander('Shared history', expanded=False):
    h1, h2, h3 = st.columns(3)
    hr = h1.selectbox('Filter region', [None]+info['regions'], format_func=lambda r: region_option(r) if r else 'All regions')
    hd = h2.date_input('Filter report date', value=None)
    hs = h3.selectbox('Filter status', ['', 'preparing', 'queued', 'running', 'awaiting_review', 'completed', 'failed', 'interrupted'])
    filters = {k: v for k, v in dict(region_id=hr['region_id'] if hr else None, report_date=hd.isoformat() if hd else None, status=hs).items() if v}
    history_key = json.dumps(filters, sort_keys=True)
    if st.session_state.get('seasonal_history_key') != history_key:
        st.session_state['seasonal_history_before'] = None
        st.session_state['seasonal_history_key'] = history_key
    before = st.session_state.get('seasonal_history_before')
    if before:
        filters['before'] = before
    try:
        history = request_json('GET', BASE + '/runs', params=filters)
        selected = st.selectbox('Analysis', history, index=None,
            format_func=lambda r: f'{r["report_date"]} · {r["region"]} · {r["status"]} · {r["id"][:8]}')
        if st.button('Open analysis', disabled=selected is None):
            remember(selected)
            st.rerun()
        hprev, hnext = st.columns(2)
        if hprev.button('Newest analyses', disabled=not before):
            st.session_state['seasonal_history_before'] = None
            st.rerun()
        if hnext.button('Older analyses', disabled=len(history) < 30):
            st.session_state['seasonal_history_before'] = history[-1]['created_at']
            st.rerun()
    except Exception as exc:
        safe_show_error(exc)

run_id = st.query_params.get('seasonal_run') or st.session_state.get('seasonal_run')
if not run_id:
    st.stop()
try:
    run = request_json('GET', f'{BASE}/runs/{run_id}')
except Exception as exc:
    safe_show_error(exc)
    st.stop()
st.subheader(f'{run["region"]} · {run["report_date"]}')
st.caption(f'Analysis {run_id} · revision {run["revision"]}. This URL can be reopened by other app users.')


def ordered(operations):
    # Oldest first; the stored map does not keep its insertion order.
    return sorted(operations.values(), key=lambda o: o['created_at'])


@st.fragment(run_every=5 if run['active'] else None)
def progress():
    current = request_json('GET', f'{BASE}/runs/{run_id}')
    if current['revision'] != run['revision']:
        st.rerun()
    st.write('Status: **' + current['status'].replace('_', ' ') + '**')
    if current['active']:
        op = current['operations'][current['active']]
        done = len(op['completed'])
        st.progress(done/len(op['stages']), text=f'{done}/{len(op["stages"])} phases completed')
        st.caption('Processing continues in the cloud if you close this page.')
    elif current['status'] in ('failed', 'interrupted'):
        last = ordered(current['operations'])[-1]
        st.error(last.get('error') or 'The operation stopped.')
        st.caption('Retry it from "Operations and review decisions" below.')
    if st.button('Refresh analysis'):
        st.rerun()
progress()

input_tab, evidence_tab, report_tab = st.tabs(['Input package', 'Evidence and analyst review', 'Report and downloads'])
with input_tab:
    selected_region = next(r for r in info['regions'] if r['region_id'] == run['region_id'])
    calendar = calendar_for(selected_region, date.fromisoformat(run['report_date']))
    with st.expander('Seasonal calendar and expected products'):
        st.dataframe(calendar, hide_index=True)
        supplied = {m['product'] for m in run['maps']}
        st.write('Product checklist')
        for p in checklist(calendar):
            st.write(('✓ ' if p in supplied else '○ ') + info['products'][p])
        st.caption('This checklist reflects declared categories. Unclassified maps are identified during extraction.')
    st.caption('PNG, JPEG or static WebP. At most 12 maps, 30 MB per file, 50 MB combined and 45 million pixels per image.')
    for m in run['maps']:
        st.write(m['name'] + ' · ' + info['products'][m['product']])
    if run['status'] == 'preparing':
        unsaved_maps = map_upload_panel(run, info, request_json)
        if st.button('Extract and review evidence', type='primary', disabled=not enabled or not run['maps'] or unsaved_maps):
            act(run, 'extract')
    if run['maps']:
        with st.expander('Input manifest'):
            st.json(request_json('GET', f'{BASE}/runs/{run_id}/input'))
    if run.get('input_artifact') and st.button('Prepare input package download'):
        try:
            link = request_json('GET', f'{BASE}/runs/{run_id}/input-package')
            st.link_button('Download input package (valid 10 minutes)', link['url'])
        except Exception as exc:
            safe_show_error(exc)

with evidence_tab:
    if run['versions']:
        version_ids = [v['id'] for v in run['versions']]
        labels = {v['id']: f'V{i} · ' + {'extraction': 'Initial extraction', 'refinement': 'Visually reviewed evidence',
                  'feedback': 'Analyst feedback revision'}[v['stage']] for i, v in enumerate(run['versions'], 1)}
        selected_version = st.selectbox('Evidence version', version_ids, index=version_ids.index(run['current_evidence']), format_func=labels.get)
        evidence = request_json('GET', f'{BASE}/runs/{run_id}/versions/{selected_version}')
        left, right = st.columns(2)
        with left:
            map_index = st.selectbox('Original map', range(len(run['maps'])), format_func=lambda i: run['maps'][i]['name'])
            original = request_bytes('GET', f'{BASE}/runs/{run_id}/maps/{map_index}')
            st.image(original, caption='Use the fullscreen control to inspect the title and legend.', width='stretch')
            st.download_button('Download original map', original, file_name=run['maps'][map_index]['name'], mime=run['maps'][map_index]['object']['mime'])
            if run['maps'][map_index]['note']:
                st.write('Analyst input note: ' + run['maps'][map_index]['note'])
        with right:
            figure_id = f'{run["report_date"]}__{run["region_id"]}__{run_id[:8]}__f{map_index+1:02}'
            evidence_panel(next(m for m in evidence['maps'] if m['figure_id'] == figure_id))
        with st.expander('Structured evidence · JSON'):
            st.json(evidence)
        if len(version_ids) > 1:
            with st.expander('Compare evidence versions'):
                comparison = st.selectbox('Compare against', [v for v in version_ids if v != selected_version], format_func=labels.get)
                prior = request_json('GET', f'{BASE}/runs/{run_id}/versions/{comparison}')
                differences({'differences': evidence_diff(prior, evidence)})
        editable = enabled and not run['active'] and selected_version == run['current_evidence']
        comments = st.text_area('Analyst comments', max_chars=20000)
        if st.button('Revise evidence', disabled=not editable or not comments.strip() or run['status'] not in ('awaiting_review', 'completed')):
            act(run, 'feedback', version_id=selected_version, comments=comments)
        confirmed = st.checkbox('I have reviewed the maps and confirm this exact evidence version.', key='confirm_'+selected_version)
        if st.button('Confirm evidence and draft report', type='primary', disabled=not editable or not confirmed or run['status'] != 'awaiting_review'):
            act(run, 'confirm', version_id=selected_version, confirmed=True)
    else:
        st.info('Open the Input package tab to upload maps and start extraction.')

with report_tab:
    if run['confirmation']:
        st.caption('Confirmed evidence: ' + run['confirmation']['version_id'])
    for op in reversed(ordered(run['operations'])):
        if op.get('artifacts'):
            with st.expander('Report · ' + op['id'][:8], expanded=bool(run['artifacts'])):
                details = request_json('GET', f'{BASE}/runs/{run_id}/operations/{op["id"]}')
                analysis_view(details['output']['report'])
                for name in op['artifacts']:
                    if st.button('Prepare download: ' + name, key=op['id']+name):
                        try:
                            link = request_json('GET', f'{BASE}/runs/{run_id}/download-link/{name}', params={'operation_id': op['id']})
                            st.link_button('Download ' + name + ' (valid 10 minutes)', link['url'])
                        except Exception as exc:
                            safe_show_error(exc)
    if not any(o.get('artifacts') for o in run['operations'].values()):
        st.info('The report and Word/ZIP exports appear after confirmation and the three drafting phases.')

if run['operations']:
    with st.expander('Operations and review decisions'):
        operations = ordered(run['operations'])
        op_id = st.selectbox('Operation', [o['id'] for o in operations], index=len(operations)-1,
            format_func=lambda oid: oid[:8]+' · '+run['operations'][oid]['action']+' · '+run['operations'][oid]['status'])
        detail = request_json('GET', f'{BASE}/runs/{run_id}/operations/{op_id}')
        op, output = detail['operation'], detail['output'] or {}
        st.json(op)
        for field in ('review', 'issue_resolutions', 'analyst_comments', 'feedback_resolutions', 'initial_analysis', 'draft_review'):
            if field in output:
                st.write(field.replace('_', ' ').capitalize())
                if field == 'initial_analysis':
                    analysis_view(output[field])
                elif field in ('review', 'draft_review'):
                    review_view(output[field])
                elif field in ('issue_resolutions', 'feedback_resolutions'):
                    review_view({'resolutions': output[field]})
                else:
                    st.write(output[field])
        if op_id == operations[-1]['id'] and op['status'] in ('failed', 'interrupted'):
            timeout = st.selectbox('Per-call timeout (seconds)', [600, 1200, 1800])
            st.caption('Retry runs ' + ', '.join(op['stages']) + ' again from the same inputs. '
                       'A call whose remote outcome was uncertain may be charged again.')
            if st.button('Retry failed operation', disabled=not enabled or bool(run['active'])):
                act(run, 'retry', operation_id=op_id, timeout=timeout)
