import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

import pandas as pd
import streamlit as st

from app.services.mfi_drafter.table_projection import build_mfi_raw_table_downloads
from app.shared.report_blocks import basket_definition_table_display
from app.streamlit_backend.dispatcher import dispatch_request

WFP_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/5/5f/WFP_Logo.svg/512px-WFP_Logo.svg.png"
WFP_PRIMARY = "#0072BC"
WFP_PRIMARY_DARK = "#005A9C"
WFP_NAVY = "#003A5D"
WFP_LIGHT = "#E6F1FA"
WFP_BG = "#EEF4FA"
WFP_TEXT = "#0C1E2E"
INSTRUCTIONS_PAGE_URL = "/How_To_Use_The_Tools"
BUG_REPORT_MESSAGE = "The second testing phase is going to start soon"
INSTRUCTIONS_PAGE_PATH = "pages/1_How_To_Use_The_Tools.py"
ONBOARDING_PAGE_PATH = "pages/0_Tester_Onboarding.py"


def apply_wfp_theme() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;500;600;700&display=swap');
        :root {
            --wfp-primary: #0072BC;
            --wfp-primary-dark: #005A9C;
            --wfp-navy: #003A5D;
            --wfp-light: #E6F1FA;
            --wfp-bg: #EEF4FA;
            --wfp-surface: #F1F6FB;
            --wfp-surface-strong: #E1ECF7;
            --wfp-border: rgba(0, 58, 93, 0.18);
            --wfp-text: #0C1E2E;
            --wfp-muted: #4B5F73;
        }
        html, body, [class*="css"] {
            font-family: 'Montserrat', sans-serif;
        }
        .stApp {
            background: radial-gradient(1200px 700px at 0% 0%, #E1ECF7 0%, #F1F6FB 45%, #E7F1FA 100%);
            color: var(--wfp-text);
        }
        main .block-container {
            padding-top: 2.5rem;
        }
        main h1, main h2, main h3, main h4, main h5, main h6 {
            color: var(--wfp-navy);
        }
        main label {
            color: var(--wfp-text);
            font-weight: 600;
        }
        label[data-testid="stWidgetLabel"],
        label[data-testid="stWidgetLabel"] * {
            color: var(--wfp-text) !important;
            opacity: 1 !important;
        }
        label[data-testid="stWidgetLabel"] p {
            color: var(--wfp-text) !important;
            opacity: 1 !important;
            font-weight: 600 !important;
        }
        div[data-testid="stMetricLabel"],
        div[data-testid="stMetricLabel"] * {
            color: rgba(0, 58, 93, 0.9) !important;
            opacity: 1 !important;
            font-weight: 700 !important;
        }
        div[data-testid="stMetricValue"],
        div[data-testid="stMetricValue"] * {
            color: var(--wfp-navy) !important;
            opacity: 1 !important;
            font-weight: 700 !important;
        }
        div[data-testid="stMetricDelta"],
        div[data-testid="stMetricDelta"] * {
            opacity: 1 !important;
        }
        div[data-testid="stSpinner"],
        div[data-testid="stSpinner"] * {
            color: var(--wfp-text) !important;
            opacity: 1 !important;
        }
        main p, main .stMarkdown {
            color: var(--wfp-text);
        }
        main .stCaption, main small {
            color: var(--wfp-muted);
        }
        div[data-testid="stForm"] {
            background: linear-gradient(160deg, var(--wfp-surface) 0%, var(--wfp-light) 100%);
            border: 1px solid var(--wfp-border);
            border-radius: 18px;
            padding: 1.5rem 1.5rem 0.75rem 1.5rem;
            box-shadow: 0 18px 36px rgba(0, 58, 93, 0.08);
        }
        section[data-testid="stSidebar"], .stSidebar {
            background: linear-gradient(180deg, var(--wfp-navy) 0%, var(--wfp-primary) 100%);
            color: #FFFFFF;
        }
        section[data-testid="stSidebar"] .stTextInput label,
        section[data-testid="stSidebar"] label,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] p {
            color: #E6F1FA;
        }
        .stButton > button,
        .stDownloadButton > button,
        div[data-testid="stFormSubmitButton"] button,
        div[data-testid^="baseButton-"] button {
            background: var(--wfp-primary) !important;
            color: #FFFFFF !important;
            border-radius: 999px !important;
            border: none !important;
            padding: 0.55rem 1.25rem !important;
            font-weight: 600 !important;
        }
        .stButton > button:hover,
        .stDownloadButton > button:hover,
        div[data-testid="stFormSubmitButton"] button:hover,
        div[data-testid^="baseButton-"] button:hover {
            background: var(--wfp-primary-dark) !important;
            color: #FFFFFF !important;
        }
        div[class*="st-key-"][class*="_report_delivery"] .stDownloadButton > button {
            min-height: 4.25rem !important;
            width: 100% !important;
            font-size: 1.08rem !important;
            justify-content: center !important;
        }
        div[class*="st-key-"][class*="_report_delivery"] .stButton > button {
            min-height: 3rem !important;
            width: 100% !important;
            justify-content: center !important;
        }
        .stTextInput input,
        .stTextArea textarea,
        .stNumberInput input,
        .stDateInput input,
        .stTimeInput input,
        .stSelectbox div[data-baseweb="select"] > div,
        .stMultiSelect div[data-baseweb="select"] > div {
            background-color: var(--wfp-surface);
            color: var(--wfp-text);
            border: 1px solid var(--wfp-border);
            border-radius: 8px;
        }
        .stTextInput input:focus,
        .stTextArea textarea:focus,
        .stNumberInput input:focus,
        .stDateInput input:focus,
        .stTimeInput input:focus,
        .stSelectbox div[data-baseweb="select"]:focus-within,
        .stMultiSelect div[data-baseweb="select"]:focus-within {
            border-color: var(--wfp-primary);
            box-shadow: 0 0 0 2px rgba(0, 114, 188, 0.18);
        }
        .stSelectbox div[data-baseweb="select"] span,
        .stMultiSelect div[data-baseweb="select"] span {
            color: var(--wfp-text) !important;
        }
        .stTextInput input::placeholder,
        .stTextArea textarea::placeholder,
        .stNumberInput input::placeholder,
        .stDateInput input::placeholder,
        .stTimeInput input::placeholder {
            color: #5A6B7B;
        }
        div[role="listbox"] {
            background-color: var(--wfp-surface);
            color: var(--wfp-text);
        }
        div[role="option"] {
            color: var(--wfp-text);
        }
        .stMultiSelect div[data-baseweb="tag"] {
            background: var(--wfp-surface-strong);
            color: var(--wfp-navy);
            border: 1px solid rgba(0, 58, 93, 0.2);
        }
        .stMultiSelect div[data-baseweb="tag"] span {
            color: var(--wfp-navy);
        }
        .stMultiSelect div[data-baseweb="tag"] svg {
            color: var(--wfp-navy);
        }
        div[data-baseweb="checkbox"] > div {
            background-color: var(--wfp-surface);
            border-color: var(--wfp-border);
        }
        div[data-testid="stCheckbox"] label,
        div[data-testid="stCheckbox"] span,
        div[data-testid="stCheckbox"] p {
            color: var(--wfp-text) !important;
            opacity: 1 !important;
            font-weight: 600 !important;
        }
        div[data-testid="stCheckbox"] small {
            color: var(--wfp-muted) !important;
            opacity: 1 !important;
        }
        section[data-testid="stSidebar"] div[data-testid="stCheckbox"] * {
            color: #E6F1FA !important;
        }
        /* File uploader: override dark dropzone + low-contrast labels */
        div[data-testid="stFileUploader"] label {
            color: var(--wfp-text) !important;
            opacity: 1 !important;
            font-weight: 600;
        }
        div[data-testid="stFileUploader"] small,
        div[data-testid="stFileUploader"] p,
        div[data-testid="stFileUploader"] span {
            color: var(--wfp-muted) !important;
            opacity: 1 !important;
        }
        section[data-testid="stFileUploaderDropzone"],
        div[data-testid="stFileUploader"] section {
            background: linear-gradient(160deg, var(--wfp-surface) 0%, var(--wfp-light) 100%) !important;
            border: 1px dashed rgba(0, 114, 188, 0.45) !important;
            border-radius: 14px !important;
            box-shadow: 0 10px 22px rgba(0, 58, 93, 0.08) !important;
        }
        section[data-testid="stFileUploaderDropzone"] *,
        div[data-testid="stFileUploader"] section * {
            color: var(--wfp-text) !important;
            opacity: 1 !important;
        }
        div[data-testid="stFileUploader"] button,
        section[data-testid="stFileUploaderDropzone"] button {
            background: var(--wfp-primary) !important;
            color: #FFFFFF !important;
            border: none !important;
            border-radius: 999px !important;
            font-weight: 600 !important;
        }
        div[data-testid="stFileUploader"] button:hover,
        section[data-testid="stFileUploaderDropzone"] button:hover {
            background: var(--wfp-primary-dark) !important;
            color: #FFFFFF !important;
        }
        /* Semantic tab roles cover both BaseWeb and React Aria Streamlit tabs. */
        .stTabs [role="tab"] {
            color: var(--wfp-text) !important;
            font-weight: 600;
        }
        .stTabs [role="tab"] [data-testid="stMarkdownContainer"],
        .stTabs [role="tab"] p {
            color: inherit !important;
        }
        .stTabs [role="tab"]:hover,
        .stTabs [role="tab"][aria-selected="true"] {
            color: var(--wfp-primary-dark) !important;
        }
        .stTabs [role="tab"]:focus-visible {
            outline: 2px solid var(--wfp-primary-dark);
            outline-offset: -2px;
        }
        .stTabs [data-baseweb="tab-highlight"],
        .stTabs .react-aria-SelectionIndicator {
            background-color: var(--wfp-primary-dark) !important;
        }
        .wfp-sidebar-logo {
            display: block;
            margin: 0.5rem auto 1rem auto;
            max-width: 180px;
        }
        button[aria-label="Tester Onboarding"] {
            height: 84px !important;
            font-size: 1.08rem !important;
            background: #1C92E6 !important;
            box-shadow: 0 18px 32px rgba(0, 58, 93, 0.28) !important;
        }
        button[aria-label="Tester Onboarding"]:hover {
            background: #007BD6 !important;
        }
        .bug-report-button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 0.45rem 1.1rem;
            border-radius: 999px;
            background: #2BA8FF;
            color: #FFFFFF !important;
            font-weight: 600;
            text-decoration: none;
            box-shadow: 0 10px 18px rgba(0, 58, 93, 0.18);
            white-space: nowrap;
        }
        .bug-report-button:hover {
            background: #1C92E6;
            color: #FFFFFF !important;
        }
        .sidebar-bug-link {
            display: block;
            text-align: center;
            background: #2BA8FF;
            color: #FFFFFF !important;
            padding: 0.55rem 1rem;
            border-radius: 999px;
            font-weight: 700;
            text-decoration: none;
            margin-top: 0.85rem;
            box-shadow: 0 10px 20px rgba(0, 58, 93, 0.22);
        }
        .sidebar-bug-link:hover {
            background: #1C92E6;
            color: #FFFFFF !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_wfp_sidebar_logo() -> None:
    base_dir = Path(__file__).resolve().parent
    logo_candidates = [
        base_dir / "assets" / "wfp_logo.png",
        base_dir / "app" / "shared" / "assets" / "wfp_logo.png",
    ]
    logo_path = next((p for p in logo_candidates if p.exists()), None)
    if logo_path is not None:
        st.image(str(logo_path), width="stretch")
    else:
        st.image(WFP_LOGO_URL, width="stretch")
    st.markdown("---")


def render_instructions_sidebar_button(
    *,
    key: str,
    label: str = "How to use the tools",
    page_path: str = INSTRUCTIONS_PAGE_PATH,
) -> None:
    st.markdown("### Help")
    if st.button(label, key=key, type="secondary", use_container_width=True):
        if hasattr(st, "switch_page"):
            st.switch_page(page_path)
            return
        st.info("Use the pages in the left sidebar to access each service.")


def render_onboarding_sidebar_button(
    *,
    key: str,
    label: str = "Tester Onboarding",
    page_path: str = ONBOARDING_PAGE_PATH,
) -> None:
    st.markdown("### Start here")
    if st.button(label, key=key, type="primary", use_container_width=True):
        if hasattr(st, "switch_page"):
            st.switch_page(page_path)
            return
        st.info("Use the pages in the left sidebar to access each section.")


def render_bug_report_header_link(
    *,
    label: str = BUG_REPORT_MESSAGE,
) -> None:
    st.markdown(
        f'<div style="text-align: right;"><span class="bug-report-button">{label}</span></div>',
        unsafe_allow_html=True,
    )


def render_bug_report_sidebar_link(
    *,
    label: str = BUG_REPORT_MESSAGE,
) -> None:
    st.markdown(
        f'<span class="sidebar-bug-link">{label}</span>',
        unsafe_allow_html=True,
    )


def quote_path_param(value: Any) -> str:
    return quote(str(value), safe="")


_DISPATCH_EXECUTOR: Optional[ThreadPoolExecutor] = None
_DISPATCH_EXECUTOR_LOCK = threading.Lock()


def _get_dispatch_executor() -> ThreadPoolExecutor:
    global _DISPATCH_EXECUTOR
    if _DISPATCH_EXECUTOR is None:
        with _DISPATCH_EXECUTOR_LOCK:
            if _DISPATCH_EXECUTOR is None:
                _DISPATCH_EXECUTOR = ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix="dispatch-deadline"
                )
    return _DISPATCH_EXECUTOR


def _dispatch_with_deadline(method: str, path: str, *, timeout: float, **kwargs: Any) -> Any:
    # A timed-out dispatch keeps running in its worker thread until it finishes
    # (bounded pool; DB-level timeouts make hung calls die within tens of
    # seconds), but the UI gets a visible error instead of an eternal spinner.
    future = _get_dispatch_executor().submit(
        lambda: dispatch_request(method, path, **kwargs)
    )
    try:
        return future.result(timeout=timeout)
    except _FutureTimeoutError:
        future.cancel()
        raise RuntimeError(
            f"{method} {path} timed out after {timeout}s waiting for the local backend; "
            "the database connection may be unavailable. Retry or reload the page."
        ) from None


def request_json(
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Any = None,
    data: Optional[dict] = None,
    files: Optional[dict] = None,
    timeout: int = 60,
) -> Any:
    resp = _dispatch_with_deadline(
        method,
        path,
        timeout=timeout,
        params=params,
        json_body=json_body,
        data=data,
        files=files,
    )
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{method} {path} failed ({resp.status_code}): {detail}")

    if not resp.content:
        return None

    try:
        return resp.json()
    except Exception:
        return resp.text


def request_bytes(method: str, path: str, *, json_body: Any = None, timeout: int = 120) -> bytes:
    resp = _dispatch_with_deadline(method, path, timeout=timeout, json_body=json_body)
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{method} {path} failed ({resp.status_code}): {detail}")
    return resp.content


@st.cache_data(show_spinner=False, ttl=3600)
def load_artifact_bytes(path: str) -> bytes:
    return request_bytes("GET", path)


def safe_show_error(err: Exception) -> None:
    st.error(str(err))


def decode_base64_data(value: Any) -> Optional[bytes]:
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.startswith("data:") and "base64," in s:
        s = s.split("base64,", 1)[1]
    try:
        return base64.b64decode(s)
    except Exception:
        return None


def render_retriever_traces(traces: Any, *, max_items: int = 200) -> None:
    if not isinstance(traces, list) or not traces:
        st.write("No logs")
        return

    lines = []
    for item in traces[-max_items:]:
        if isinstance(item, (dict, list)):
            try:
                lines.append(json.dumps(item, ensure_ascii=False))
            except Exception:
                lines.append(str(item))
        else:
            lines.append(str(item))

    st.code("\n".join(lines))


def ordered_live_output_sections(live_outputs: Any) -> List[Tuple[str, Dict[str, Any]]]:
    if not isinstance(live_outputs, dict):
        return []

    ordered_keys = ["databridges", "seerist", "reliefweb"]
    sections: List[Tuple[str, Dict[str, Any]]] = []
    for key in ordered_keys:
        payload = live_outputs.get(key)
        if isinstance(payload, dict):
            sections.append((key, payload))
    for key, payload in live_outputs.items():
        if key in ordered_keys or not isinstance(payload, dict):
            continue
        sections.append((str(key), payload))
    return sections


def render_live_outputs(
    live_outputs: Any,
    *,
    key_prefix: str,
    render_instance_id: Optional[str] = None,
    enable_downloads: bool = True,
) -> None:
    sections = ordered_live_output_sections(live_outputs)
    if not sections:
        return

    suffix = f"-{render_instance_id}" if render_instance_id else ""
    st.markdown("#### Live Retrieval Outputs")
    for section_name, section in sections:
        kind = str(section.get("kind") or "")
        title = str(section.get("title") or section_name.replace("_", " ").title())
        expanded = section_name == "databridges"
        with st.expander(title, expanded=expanded):
            status = str(section.get("status") or "")
            summary = str(section.get("summary") or "")
            count = int(section.get("count") or 0)
            updated_at = str(section.get("updated_at") or "")
            st.caption(
                " | ".join(
                    part for part in [f"Status: {status}", f"Count: {count}", f"Updated: {updated_at}"] if part
                )
            )
            if summary:
                if status == "failed":
                    st.warning(summary)
                else:
                    st.write(summary)

            if kind == "table":
                render_live_table_section(
                    section,
                    key_prefix=f"{key_prefix}-{section_name}{suffix}",
                    enable_downloads=enable_downloads,
                )
            elif kind == "documents":
                render_live_document_section(
                    section,
                    key_prefix=f"{key_prefix}-{section_name}{suffix}",
                    enable_downloads=enable_downloads,
                )
            else:
                st.json(section)


def render_live_table_section(
    section: Dict[str, Any],
    *,
    key_prefix: str,
    enable_downloads: bool = True,
) -> None:
    columns = section.get("columns")
    rows_preview = section.get("rows_preview")
    if isinstance(columns, list) and isinstance(rows_preview, list):
        preview_df = pd.DataFrame(rows_preview, columns=[str(column) for column in columns])
        if not preview_df.empty:
            st.dataframe(preview_df, use_container_width=True, hide_index=True)
        else:
            st.write("No preview rows available.")

    artifacts = section.get("download_artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        return

    if not enable_downloads:
        st.caption("Downloads will appear once the run reaches a final status.")
        return

    st.markdown("**Downloads**")
    for idx, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict):
            continue
        download_path = str(artifact.get("download_path") or "")
        if not download_path:
            continue
        try:
            content = load_artifact_bytes(download_path)
        except Exception as err:
            st.caption(f"Download unavailable: {err}")
            continue

        st.download_button(
            label=str(artifact.get("label") or f"Download {idx + 1}"),
            data=content,
            file_name=str(artifact.get("file_name") or f"download-{idx + 1}.bin"),
            mime=str(artifact.get("mime_type") or "application/octet-stream"),
            key=f"{key_prefix}-artifact-{idx}",
            use_container_width=True,
        )


def render_live_document_section(
    section: Dict[str, Any],
    *,
    key_prefix: str,
    enable_downloads: bool = True,
) -> None:
    documents = section.get("documents")
    if not isinstance(documents, list) or not documents:
        st.write("No documents available.")
        return

    for idx, document in enumerate(documents):
        if not isinstance(document, dict):
            continue
        title = str(document.get("title") or document.get("doc_id") or f"Document {idx + 1}")
        date = str(document.get("date") or "")
        source = str(document.get("source") or "")
        header = " | ".join(part for part in [title, source, date] if part)
        with st.expander(header or f"Document {idx + 1}", expanded=False):
            url = str(document.get("url") or "")
            excerpt = str(document.get("excerpt") or "")
            if url:
                st.markdown(f"[Open source]({url})")
            if excerpt:
                st.write(excerpt)

            download_specs = [
                (
                    "JSON",
                    str(document.get("download_json_path") or ""),
                    str(document.get("download_json_file_name") or "document.json"),
                    "application/json",
                    f"{key_prefix}-json-{idx}",
                ),
                (
                    "TXT",
                    str(document.get("download_text_path") or ""),
                    str(document.get("download_text_file_name") or "document.txt"),
                    "text/plain",
                    f"{key_prefix}-txt-{idx}",
                ),
            ]
            if not enable_downloads:
                st.caption("Downloads will appear once the run reaches a final status.")
                continue
            cols = st.columns(2)
            for col, (label, download_path, file_name, mime, button_key) in zip(cols, download_specs):
                if not download_path:
                    continue
                try:
                    content = load_artifact_bytes(download_path)
                except Exception as err:
                    col.caption(f"{label} unavailable: {err}")
                    continue
                col.download_button(
                    label=f"Download {label}",
                    data=content,
                    file_name=file_name,
                    mime=mime,
                    key=button_key,
                    use_container_width=True,
                )


def render_results_tabs(
    *,
    summary: Callable[[], None],
    json_data: Any,
    visuals: Optional[Callable[[], None]] = None,
    export: Optional[Callable[[], None]] = None,
) -> None:
    tab_summary, tab_json, tab_visuals, tab_export = st.tabs(["Summary", "JSON", "Visuals", "Export"])

    with tab_summary:
        summary()

    with tab_json:
        if json_data is None:
            st.write("No data")
        else:
            st.json(json_data)

    with tab_visuals:
        if visuals is None:
            st.write("No visuals")
        else:
            visuals()

    with tab_export:
        if export is None:
            st.write("No export available")
        else:
            export()


def render_report_delivery(
    *,
    run_id: str,
    key_prefix: str,
    export_path: str,
    file_name: str,
    render_preview: Callable[[], None],
    render_technical_details: Optional[Callable[[], None]] = None,
    labels: Optional[Dict[str, str]] = None,
) -> None:
    """Render a download-first result experience for completed report runs."""
    text = {
        "ready": "Report ready",
        "ready_caption": "Your report has been generated. Download the Word file or preview it on this page.",
        "preparing": "Preparing your download...",
        "download": "Download report (.docx)",
        "view": "View report on this page",
        "hide": "Hide report preview",
        "technical": "Technical details",
        "export_error": "The Word file could not be prepared. You can still preview the report below.",
        "retry": "Retry preparing download",
    }
    text.update(labels or {})

    docx_bytes_key = f"{key_prefix}_docx_bytes"
    docx_run_id_key = f"{key_prefix}_docx_run_id"
    docx_error_key = f"{key_prefix}_docx_error"
    docx_error_run_id_key = f"{key_prefix}_docx_error_run_id"
    preview_key = f"{key_prefix}_report_preview_{run_id}"

    docx_bytes = None
    if st.session_state.get(docx_run_id_key) == run_id:
        docx_bytes = st.session_state.get(docx_bytes_key)

    export_error = None
    if st.session_state.get(docx_error_run_id_key) == run_id:
        export_error = st.session_state.get(docx_error_key)

    if docx_bytes is None and not export_error:
        with st.spinner(text["preparing"]):
            try:
                docx_bytes = request_bytes("POST", export_path, json_body={}, timeout=300)
            except Exception as err:
                export_error = str(err)
                st.session_state[docx_error_key] = export_error
                st.session_state[docx_error_run_id_key] = run_id
            else:
                st.session_state[docx_bytes_key] = docx_bytes
                st.session_state[docx_run_id_key] = run_id
                st.session_state.pop(docx_error_key, None)
                st.session_state.pop(docx_error_run_id_key, None)

    with st.container(border=True, key=f"{key_prefix}_report_delivery"):
        st.subheader(text["ready"])
        st.caption(text["ready_caption"])

        if docx_bytes:
            st.download_button(
                text["download"],
                data=docx_bytes,
                file_name=file_name,
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                key=f"{key_prefix}_download_docx_{run_id}",
                type="primary",
                width="stretch",
                on_click="ignore",
            )
        else:
            st.error(text["export_error"])
            if st.button(
                text["retry"],
                key=f"{key_prefix}_retry_docx_{run_id}",
                width="stretch",
            ):
                st.session_state.pop(docx_error_key, None)
                st.session_state.pop(docx_error_run_id_key, None)
                st.rerun()

        preview_open = bool(st.session_state.get(preview_key, False))
        if st.button(
            text["hide"] if preview_open else text["view"],
            key=f"{key_prefix}_toggle_preview_{run_id}",
            width="stretch",
        ):
            preview_open = not preview_open
            st.session_state[preview_key] = preview_open

    if preview_open:
        render_preview()

    if render_technical_details is not None:
        with st.expander(text["technical"], expanded=False):
            render_technical_details()


def _llm_elapsed_seconds(call: Dict[str, Any]) -> Optional[float]:
    started_at = call.get("started_at")
    if not isinstance(started_at, str) or not started_at.strip():
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
    except (TypeError, ValueError):
        return None


def render_llm_diagnostics(diagnostics: Any, *, live: bool = False) -> None:
    """Render only the sanitized public LLM trace contract."""
    if not isinstance(diagnostics, dict) or not diagnostics:
        return
    calls = [item for item in diagnostics.get("calls") or [] if isinstance(item, dict)]
    current_id = str(diagnostics.get("current_call_id") or "")
    current_call = next(
        (item for item in reversed(calls) if str(item.get("call_id") or "") == current_id),
        None,
    )

    if live and current_call is not None:
        operation = str(current_call.get("operation") or "LLM request")
        elapsed = _llm_elapsed_seconds(current_call)
        elapsed_label = f"{elapsed:.1f}s" if elapsed is not None else "in progress"
        st.info(f"Current LLM operation: {operation} ({elapsed_label})")

    counters = st.columns(4)
    counters[0].metric("Completed LLM calls", str(diagnostics.get("succeeded_calls") or 0))
    counters[1].metric("Recovered JSON calls", str(diagnostics.get("recovered_calls") or 0))
    counters[2].metric("Failed LLM calls", str(diagnostics.get("failed_calls") or 0))
    counters[3].metric(
        "Contract failures", str(diagnostics.get("contract_failed_calls") or 0)
    )

    failures = [item for item in calls if item.get("status") == "failed"]
    if failures:
        failure = failures[-1]
        message = (
            "LLM call failed: "
            f"{failure.get('failure_code') or 'llm_call_failed'}; "
            f"node={failure.get('node') or 'unknown'}; "
            f"operation={failure.get('operation') or 'unknown'}; "
            f"call_id={failure.get('call_id') or 'unknown'}."
        )
        st.error(message)

    if not live and calls:
        rows = []
        for call in calls:
            rows.append(
                {
                    "Call": call.get("call_id"),
                    "Node": call.get("node"),
                    "Operation": call.get("operation"),
                    "Artifact": call.get("artifact_id") or call.get("artifact_type") or "—",
                    "Attempt": call.get("correction_attempt") or 0,
                    "Status": call.get("status"),
                    "Duration (ms)": call.get("duration_ms"),
                    "Model": call.get("model"),
                    "Failure code": call.get("failure_code") or "—",
                    "JSON roots": call.get("json_root_value_count"),
                    "Trailing chars": call.get("json_trailing_character_count"),
                    "Disposition": call.get("disposition") or "—",
                }
            )
        st.dataframe(rows, hide_index=True, width="stretch")
        if diagnostics.get("payload_persistence_failures"):
            st.warning(
                "One or more private LLM trace payloads could not be persisted. "
                "The report result itself is unchanged."
            )


def render_run_status(
    status: Any,
    *,
    render_instance_id: Optional[str] = None,
    enable_downloads: bool = True,
) -> None:
    if not isinstance(status, dict):
        st.write(status)
        return

    progress = int(status.get("progress_pct") or 0)
    progress = min(max(progress, 0), 100)

    cols = st.columns(3)
    cols[0].metric("Status", str(status.get("status") or ""))
    cols[1].metric("Node", str(status.get("current_node") or ""))
    cols[2].metric("Progress", f"{progress}%")

    st.progress(progress / 100)

    warnings = status.get("warnings") or []
    if isinstance(warnings, list) and warnings:
        with st.expander("Warnings", expanded=False):
            for w in warnings:
                st.write(f"- {w}")

    metadata = status.get("metadata")
    if isinstance(metadata, dict) and metadata:
        llm_diagnostics = metadata.get("llm_diagnostics")
        if isinstance(llm_diagnostics, dict) and llm_diagnostics:
            render_llm_diagnostics(llm_diagnostics, live=True)

        live_outputs = metadata.get("live_outputs")
        if isinstance(live_outputs, dict) and live_outputs:
            render_live_outputs(
                live_outputs,
                key_prefix=str(status.get("run_id") or status.get("current_node") or "run"),
                render_instance_id=render_instance_id,
                enable_downloads=enable_downloads,
            )

        retriever_traces = metadata.get("retriever_traces")
        if isinstance(retriever_traces, list) and retriever_traces:
            with st.expander("Logs", expanded=False):
                render_retriever_traces(retriever_traces)

        with st.expander("Metadata", expanded=False):
            st.json(metadata)

    if status.get("error"):
        st.error(str(status.get("error")))

    if status.get("traceback"):
        with st.expander("Traceback", expanded=False):
            st.code(str(status.get("traceback")))


def run_async_and_poll(
    *,
    start_method: str,
    start_path: str,
    status_path_template: str,
    result_path_template: str,
    start_json: Any = None,
    start_data: Optional[dict] = None,
    start_files: Optional[dict] = None,
    poll_interval_seconds: float = 2.0,
    timeout_seconds: int = 1800,
    on_started: Any = None,
) -> Tuple[str, Any, Any]:
    start_resp = request_json(
        start_method,
        start_path,
        json_body=start_json,
        data=start_data,
        files=start_files,
        timeout=60,
    )

    if not isinstance(start_resp, dict) or "run_id" not in start_resp:
        raise RuntimeError(f"Unexpected start response: {start_resp}")

    run_id = str(start_resp.get("run_id"))
    if on_started is not None:
        on_started(run_id)

    status_placeholder = st.empty()
    started = time.time()
    last_status: Any = None
    render_count = 0

    while True:
        status = request_json("GET", status_path_template.format(run_id=run_id), timeout=30)
        last_status = status
        is_terminal = isinstance(status, dict) and status.get("status") in {"completed", "failed"}
        with status_placeholder.container():
            render_run_status(
                status,
                render_instance_id=None if is_terminal else f"poll-{render_count}",
                enable_downloads=is_terminal,
            )
        render_count += 1

        if is_terminal:
            break

        if time.time() - started > timeout_seconds:
            raise RuntimeError("Polling timeout")

        time.sleep(poll_interval_seconds)

    result: Any = None
    if isinstance(last_status, dict) and last_status.get("status") == "completed":
        result = request_json("GET", result_path_template.format(run_id=run_id), timeout=120)

    return run_id, last_status, result


def render_visualizations(visualizations: Any) -> None:
    if not isinstance(visualizations, dict) or not visualizations:
        st.write("No visualizations")
        return

    ids = [k for k in visualizations.keys() if isinstance(k, str)]
    for legacy_id, canonical_id in (
        ("food_basket_trend", "food_basket_trend_primary"),
        ("regional_comparison", "regional_comparison_primary"),
    ):
        if canonical_id in ids and legacy_id in ids:
            ids.remove(legacy_id)
    ids.sort()

    for fig_id in ids:
        img_b64 = visualizations.get(fig_id)
        img_bytes = decode_base64_data(img_b64)
        if img_bytes is None:
            continue
        st.subheader(fig_id)
        st.image(img_bytes, width="stretch")


def render_report_sections(sections: Any) -> None:
    if not isinstance(sections, dict) or not sections:
        st.write("No report sections")
        return

    keys = [k for k in sections.keys() if isinstance(k, str)]
    keys.sort()

    for k in keys:
        content = sections.get(k)
        with st.expander(k, expanded=False):
            if isinstance(content, str):
                st.markdown(content)
            else:
                st.write(content)


def render_mfi_raw_table_downloads(
    assessment_profile: Any,
    *,
    key_prefix: str,
) -> None:
    """Render the complete canonical table bundle in Technical details."""
    if not isinstance(assessment_profile, dict):
        return
    st.markdown("**Complete analytical table downloads**")
    st.caption(
        "These files contain the complete unprojected Phase 2 tables at canonical "
        "precision; reader-facing report tables are selected and formatted projections."
    )
    for index, download in enumerate(
        build_mfi_raw_table_downloads(assessment_profile)
    ):
        st.download_button(
            str(download["label"]),
            data=download["data"],
            file_name=str(download["file_name"]),
            mime=str(download["mime"]),
            key=f"{key_prefix}_raw_table_{index}",
        )


def render_report_blocks(blocks: Any, visualizations: Any = None) -> None:
    if not isinstance(blocks, list):
        st.write(blocks)
        return

    viz = visualizations if isinstance(visualizations, dict) else {}

    for idx, block in enumerate(blocks):
        if not isinstance(block, dict):
            st.write(block)
            continue

        btype = block.get("type")
        meta = block.get("meta")
        layout = (
            meta.get("mfi_layout")
            if isinstance(meta, dict)
            and isinstance(meta.get("mfi_layout"), dict)
            else {}
        )

        if btype == "heading":
            text = str(block.get("text") or "")
            level = int(block.get("level") or 2)
            if level <= 1:
                st.title(text)
            elif level == 2:
                if layout.get("role") == "major_section" and idx > 1:
                    divider = getattr(st, "divider", None)
                    if callable(divider):
                        divider()
                st.header(text)
            elif level == 3:
                st.subheader(text)
            else:
                st.markdown(f"**{text}**")
            continue

        if btype == "paragraph":
            text = str(block.get("text") or "")
            if text.strip():
                st.markdown(text)
            continue

        if btype == "figure":
            fig_id = block.get("figure_id")
            caption = block.get("caption")
            if isinstance(fig_id, str) and fig_id in viz:
                img_bytes = decode_base64_data(viz.get(fig_id))
                if img_bytes is not None:
                    st.image(img_bytes, caption=caption, width="stretch")
            continue

        if btype == "references":
            refs = block.get("references")
            if isinstance(refs, list) and refs:
                for r in refs:
                    if isinstance(r, dict):
                        title = r.get("title") or r.get("doc_title") or r.get("doc_id") or "Reference"
                        url = r.get("url")
                        source = r.get("source")
                        date = r.get("date")
                        parts = [p for p in [source, date] if p]
                        label = title if not parts else f"{title} ({', '.join([str(p) for p in parts])})"
                        if url:
                            st.markdown(f"- [{label}]({url})")
                        else:
                            st.markdown(f"- {label}")
                    else:
                        st.markdown(f"- {r}")
            continue

        if btype == "limitation_box":
            text = str(block.get("text") or "").strip()
            if text:
                st.warning(text)
            continue

        if btype == "methodology_note":
            text = str(block.get("text") or "").strip()
            if text:
                st.info(text)
            continue

        if btype == "table":
            if isinstance(meta, dict) and meta.get("table_kind") == "basket_definitions":
                headers, rows = basket_definition_table_display(meta)
                if headers and rows:
                    st.dataframe(
                        pd.DataFrame(rows, columns=headers),
                        width="stretch",
                        hide_index=True,
                    )
                continue
            if isinstance(meta, dict) and meta.get("table_kind") == "mfi_presentation":
                rows = meta.get("rows") or []
                columns = [str(column) for column in meta.get("columns", []) or []]
                column_specs = meta.get("column_specs") or []
                if not str(meta.get("spec_id") or "").strip() or not columns:
                    raise ValueError(
                        "Projected MFI tables require a spec ID and explicit columns"
                    )
                if not isinstance(column_specs, list) or [
                    str(item.get("key") or "") if isinstance(item, dict) else ""
                    for item in column_specs
                ] != columns:
                    raise ValueError(
                        "Projected MFI table columns do not match column_specs"
                    )
                display_rows = []
                labels = [str(item.get("label") or "") for item in column_specs]
                for row in rows:
                    values = row.get("values") if isinstance(row, dict) else None
                    if not isinstance(values, dict) or any(
                        column not in values for column in columns
                    ):
                        raise ValueError("Projected MFI table contains a malformed row")
                    display_rows.append([values[column] for column in columns])
                if display_rows:
                    title = str(meta.get("title") or "").strip()
                    if title:
                        st.markdown(f"**{title}**")
                    dataframe = pd.DataFrame(display_rows, columns=labels)
                    st.dataframe(dataframe, width="stretch", hide_index=True)
                continue

            if isinstance(meta, dict):
                st.json(meta)
            else:
                st.write(meta)
            continue

        st.write(block)
