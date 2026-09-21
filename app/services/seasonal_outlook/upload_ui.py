"""Batch selection in the UI over the existing revision-checked map uploads."""
import hashlib
import json
from datetime import date

import streamlit as st

from .inputs import inspect_image, suggested_product


def pending_maps(files, saved, limits):
    """Validate the whole selection before writing, excluding persisted images."""
    saved_hashes = {m['object']['sha256'] for m in saved}
    selected_hashes, pending = set(), []
    for file in files:
        data = file.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        if digest in selected_hashes:
            raise ValueError(f'Duplicate image in the selection: {file.name}. Remove the extra copy.')
        selected_hashes.add(digest)
        if digest not in saved_hashes:
            pending.append(dict(file=file, data=data, sha256=digest))
    if len(saved) + len(pending) > limits['maps']:
        raise ValueError(f'Use at most {limits["maps"]} maps, including those already saved.')
    total = sum(m['object']['size'] for m in saved) + sum(len(m['data']) for m in pending)
    if total > limits['total_bytes']:
        raise ValueError('Use at most 50 MB combined, including maps already saved.')
    for item in pending:
        inspect_image(item['data'], item['file'].name)
    return pending


def map_upload_panel(run, info, request_json):
    """Return whether unsaved/invalid selected files should block extraction."""
    prefix = f'seasonal_maps_{run["id"]}'
    generation = st.session_state.get(prefix + '_generation', 0)
    notice = st.session_state.pop(prefix + '_notice', None)
    if notice:
        kind, message = notice
        if kind == 'error':
            st.error(message)
            st.info('Maps already saved are retained. Check the selection, then save again to upload only the remaining maps.')
        else:
            st.success(message)

    files = st.file_uploader('Map images — select all maps at once',
        type=['png', 'jpg', 'jpeg', 'webp'], accept_multiple_files=True,
        max_upload_size=30, disabled=not info['enabled'], key=f'{prefix}_{generation}')
    st.caption('Gemini identifies map products and reads titles, legends and periods during extraction. '
               'You can leave all optional map details unchanged.')
    pending, invalid = [], False
    try:
        pending = pending_maps(files, run['maps'], info['limits'])
    except ValueError as exc:
        st.error(str(exc))
        invalid = True

    if files and not invalid:
        saved_count = len(files) - len(pending)
        if saved_count:
            st.info(f'{saved_count} selected map(s) are already saved and will not be uploaded again.')
        st.caption(f'{len(pending)} new map(s) selected · {len(run["maps"])} map(s) already saved.')

    products = list(info['products'])
    with st.form(prefix + '_details'):
        for index, item in enumerate(pending, 1):
            file = item['file']
            widget_key = f'{prefix}_{generation}_{item["sha256"]}'
            with st.expander(f'Map {index} · {file.name} · optional details', expanded=False):
                st.image(item['data'], width=380)
                suggestion = suggested_product(file.name)
                product = st.selectbox('Product category (optional)', products,
                    index=products.index(suggestion), format_func=info['products'].get, key=widget_key + '_product')
                if suggestion != 'other':
                    st.caption('Category suggested from the filename prefix. Check it against the map.')
                else:
                    st.caption('Leave “Other / not yet identified” to let Gemini identify the product during extraction.')
                issue = st.date_input('Issue date (optional)', value=None,
                    max_value=date.fromisoformat(run['report_date']), key=widget_key + '_issue')
                note = st.text_area('Map note (optional)', max_chars=10000, key=widget_key + '_note')
                item['metadata'] = dict(product=product, issue_date=issue.isoformat() if issue else '', note=note)
        save = st.form_submit_button('Save selected maps', disabled=not info['enabled'] or invalid or not pending)

    if save:
        # Keep selection/widget identity after partial failure. Only a fully
        # acknowledged batch clears the uploader; the next render reads saved hashes.
        current = run
        progress = st.progress(0, text=f'Saving {len(pending)} map(s)…')
        try:
            for index, item in enumerate(pending, 1):
                file, metadata = item['file'], item['metadata']
                identity = json.dumps([run['id'], current['revision'], 'map',
                    dict(**metadata, sha256=item['sha256'])], sort_keys=True)
                body = dict(**metadata, request_id=hashlib.sha256(identity.encode()).hexdigest(),
                            expected_revision=current['revision'])
                current = request_json('POST', f'/seasonal-outlook/runs/{run["id"]}/maps', data=body,
                    files={'file': (file.name, item['data'], file.type)}, timeout=120)
                progress.progress(index / len(pending), text=f'Saved {index} of {len(pending)} maps')
        except Exception as exc:
            st.session_state[prefix + '_notice'] = ('error', f'Upload stopped: {exc}')
        else:
            st.session_state[prefix + '_generation'] = generation + 1
            st.session_state[prefix + '_notice'] = ('success', f'{len(pending)} map(s) saved. You can now start extraction.')
        st.rerun()

    if pending or invalid:
        st.caption('Save or remove the selected maps before starting extraction.')
    return bool(pending) or invalid
