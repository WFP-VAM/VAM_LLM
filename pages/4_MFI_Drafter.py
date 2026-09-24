import streamlit as st

from streamlit_shared import (
    apply_wfp_theme,
    render_bug_report_header_link,
    render_bug_report_sidebar_link,
    render_instructions_sidebar_button,
    render_onboarding_sidebar_button,
    render_mfi_raw_table_downloads,
    render_llm_diagnostics,
    render_report_delivery,
    render_report_blocks,
    render_wfp_sidebar_logo,
    request_json,
    run_async_and_poll,
    safe_show_error,
)

st.set_page_config(page_title="MFI Drafter", layout="wide")
apply_wfp_theme()

with st.sidebar:
    render_wfp_sidebar_logo()
    render_onboarding_sidebar_button(key="sidebar_onboarding_mfi_drafter")
    render_instructions_sidebar_button(key="sidebar_instructions_mfi_drafter")
    render_bug_report_sidebar_link()

title_col, bug_col = st.columns([3, 1])
with title_col:
    st.title("MFI Report Generator")
with bug_col:
    render_bug_report_header_link()

st.caption(
    "Upload the final processed/elaborated MFI CSV produced by DataBridges. "
    "The file must include valid country, StartDate, and EndDate metadata."
)

try:
    service_info = request_json("GET", "/mfi-drafter/info", timeout=30)
except Exception:
    service_info = {}
release_control = (
    service_info.get("release_control")
    if isinstance(service_info, dict)
    else {}
) or {}
generation_enabled = bool(
    isinstance(service_info, dict)
    and service_info.get("generation_enabled")
    and release_control.get("analysis_version") == "2"
)
if not generation_enabled:
    st.warning(
        "MFI Drafter 2.0 generation is not enabled in this deployment. "
        "Existing completed reports and downloads remain available."
    )


@st.dialog("Required CSV data is missing")
def _show_csv_validation_dialog(validation):
    missing_fields = validation.get("missing_metadata_fields") or validation.get("missing_columns") or []
    if missing_fields:
        st.write("The following required fields are missing, empty, or invalid:")
        for field in missing_fields:
            st.markdown(f"- **{field}**")
    for error in validation.get("errors") or []:
        st.error(str(error))
    st.info("Please upload a corrected final processed MFI CSV from DataBridges and try again.")


with st.form("mfi_drafter_csv"):
    uploaded = st.file_uploader("Processed MFI CSV", type=["csv"], key="mfi_drafter_csv_file")
    run_csv = st.form_submit_button(
        "Generate report",
        type="primary",
        width="stretch",
        disabled=not generation_enabled,
    )

def _remember_run(run_id):
    st.session_state["mfi_last_run_id"] = run_id
    st.session_state.pop("mfi_last_result", None)
    st.query_params["mfi_run"] = run_id


if run_csv:
    try:
        if uploaded is None:
            st.error("Please upload a processed MFI CSV file")
        else:
            files = {
                "file": (
                    uploaded.name,
                    uploaded.getvalue(),
                    uploaded.type or "text/csv",
                )
            }
            validation = request_json(
                "POST",
                "/mfi-drafter/validate-csv",
                files=files,
                timeout=60,
            )
            if not isinstance(validation, dict) or not validation.get("valid"):
                _show_csv_validation_dialog(
                    validation
                    if isinstance(validation, dict)
                    else {"errors": ["The CSV could not be validated."]}
                )
            else:
                run_id, final_status, result = run_async_and_poll(
                    start_method="POST",
                    start_path="/mfi-drafter/generate-from-csv-async",
                    status_path_template="/mfi-drafter/status/{run_id}",
                    result_path_template="/mfi-drafter/result/{run_id}",
                    start_data={},
                    start_files=files,
                    poll_interval_seconds=2.0,
                    timeout_seconds=3600,
                    on_started=_remember_run,
                )
                st.session_state["mfi_last_result"] = result
                st.session_state["mfi_last_run_id"] = run_id
                for key in (
                    "mfi_docx_bytes",
                    "mfi_docx_run_id",
                    "mfi_docx_error",
                    "mfi_docx_error_run_id",
                ):
                    st.session_state.pop(key, None)
                if isinstance(final_status, dict) and final_status.get("status") == "failed":
                    st.error(final_status.get("error") or "failed")
    except Exception as e:
        safe_show_error(e)

result = st.session_state.get("mfi_last_result")
run_id = st.session_state.get("mfi_last_run_id")
if not run_id and st.query_params.get("mfi_run"):
    run_id = str(st.query_params["mfi_run"])
    st.session_state["mfi_last_run_id"] = run_id


@st.fragment(run_every="10s")
def _progress_panel(active_run):
    if not active_run:
        return
    try:
        status = request_json("GET", f"/mfi-drafter/status/{active_run}", timeout=30)
        if status.get("status") == "completed":
            if st.session_state.get("mfi_last_result") is None:
                st.session_state["mfi_last_result"] = request_json("GET", f"/mfi-drafter/result/{active_run}", timeout=120)
                st.rerun()
            return
        st.caption(f"Run {active_run} · {status.get('status')} · {status.get('current_node') or 'starting'}")
        diagnostics = (status.get("metadata") or {}).get("generation_diagnostics") or {}
        if diagnostics.get("phases"):
            st.progress(int(diagnostics.get("progress_pct", 0)) / 100)
            st.dataframe(diagnostics["phases"], hide_index=True)
        if status.get("error"):
            st.warning(status["error"])
        if status.get("status") == "failed":
            st.caption("This report could not be completed. Generate it again from the CSV.")
    except Exception as exc:
        safe_show_error(exc)


_progress_panel(run_id)

if isinstance(result, dict):

    display_run_id = str(run_id or result.get("run_id") or "")
    methodology_warnings = result.get("methodology_warnings") or []
    excluded_records = result.get("excluded_market_records") or []
    assessment_profile = result.get("assessment_profile") or {}
    limitations = assessment_profile.get("limitations") or []
    is_light = result.get("workflow_revision") == "mfi-light-v1"
    qa_review = result.get("qa_review") or {}
    context_status = result.get("context_status") or {}

    overview_columns = st.columns(3)
    mean_score = result.get("mean_mfi_across_assessed_markets")
    overview_columns[0].metric(
        "Mean MFI across assessed markets",
        f"{float(mean_score):.2f}/10" if mean_score is not None else "—",
    )
    priority_dimensions = assessment_profile.get("priority_dimension_names") or []
    overview_columns[1].metric(
        "Priority dimensions",
        str(len(priority_dimensions)),
        help=", ".join(str(item) for item in priority_dimensions),
    )
    overview_columns[2].metric(
        "Narrative QA",
        str((result.get("review_status") if is_light else qa_review.get("status")) or "not recorded").replace("_", " ").title(),
    )
    if priority_dimensions:
        st.info("Priority dimensions: " + ", ".join(priority_dimensions))

    if methodology_warnings or excluded_records or limitations:
        st.subheader("Methodology and coverage notices")
        for warning in methodology_warnings:
            message = warning.get("message") if isinstance(warning, dict) else warning
            if message:
                st.warning(str(message))
        if excluded_records:
            excluded_names = [
                str(record.get("market_name"))
                for record in excluded_records
                if isinstance(record, dict) and record.get("market_name")
            ]
            if excluded_names:
                st.caption("Excluded MFIr-only records: " + ", ".join(excluded_names))
        for limitation in limitations:
            message = (
                limitation.get("message")
                if isinstance(limitation, dict)
                else limitation
            )
            if message:
                st.warning(str(message))
    material_qa_flags = [
        flag
        for flag in qa_review.get("flags", []) or []
        if isinstance(flag, dict) and flag.get("severity") in {"high", "medium"}
    ]
    all_qa_flags = [
        flag
        for flag in qa_review.get("flags", []) or []
        if isinstance(flag, dict)
    ]
    severity_counts = {
        severity: sum(flag.get("severity") == severity for flag in all_qa_flags)
        for severity in ("high", "medium", "low")
    }
    diagnostics = result.get("generation_diagnostics") or {}
    substitutions = diagnostics.get("claim_substitutions", []) or []
    unverified_figure_claim_count = int(
        diagnostics.get("unverified_figure_claim_count", 0) or 0
    )
    if is_light:
        review_columns = st.columns(3)
        for index, family in enumerate(("dimensions", "markets")):
            review = (result.get("review_reports") or {}).get(family, {})
            review_columns[index].metric(f"{family.title()} review", "Corrected" if review.get("needs_revision") else "No changes requested")
        review_columns[2].metric("Model calls", str(result.get("llm_calls", 0)))
    else:
        qa_columns = st.columns(6)
        qa_columns[0].metric("High QA findings", str(severity_counts["high"]))
        qa_columns[1].metric("Medium QA findings", str(severity_counts["medium"]))
        qa_columns[2].metric("Low QA findings", str(severity_counts["low"]))
        qa_columns[3].metric(
            "Correction cycles", str(qa_review.get("correction_attempts") or 0)
        )
        qa_columns[4].metric("Withdrawn drafts", str(len(substitutions)))
        qa_columns[5].metric(
            "Figures to check", str(unverified_figure_claim_count)
        )
    if material_qa_flags:
        claim_scoped = any(
            isinstance(block, dict) and block.get("type") == "claim_warning"
            for block in result.get("report_blocks", []) or []
        )
        qa_location = (
            "Review the claim-level notices and final QA findings table."
            if claim_scoped
            else "Review the process-level notices and final QA findings table."
        )
        if qa_review.get("status") == "delivered_with_unverified_figures":
            st.warning(
                "Delivered with unverified figures. Check every marked figure "
                f"against the source data before operational use. {qa_location}"
            )
        else:
            st.error(
                "Narrative QA completed with unresolved material issues. "
                + qa_location
            )

    def _preview() -> None:
        render_report_blocks(result.get("report_blocks"), visualizations=result.get("visualizations"))

    def _technical_details() -> None:
        cols = st.columns(4)
        cols[0].metric("Run ID", display_run_id)
        cols[1].metric("Country", str(result.get("country") or ""))
        cols[2].metric(
            "Assessed markets",
            str(assessment_profile.get("assessed_market_count") or 0),
        )
        cols[3].metric("LLM Calls", str(result.get("llm_calls") or 0))
        st.markdown("**Release control**")
        st.json(result.get("release_control") or release_control)
        st.markdown("**Generation diagnostics**")
        if is_light:
            st.dataframe(diagnostics.get("phases", []), hide_index=True)
            for family, review in (result.get("review_reports") or {}).items():
                st.markdown(f"**{family.title()} review report**")
                st.markdown(review.get("review_markdown", ""))
        else:
            task_columns = st.columns(4)
            task_columns[0].metric(
                "Draft batches",
                (
                    f"{diagnostics.get('draft_batches_completed', 0)}/"
                    f"{diagnostics.get('draft_batches_total', 0)}"
                ),
            )
            task_columns[1].metric(
                "Semantic reviews",
                (
                    f"{diagnostics.get('semantic_reviews_completed', 0)}/"
                    f"{diagnostics.get('semantic_reviews_total', 0)}"
                ),
            )
            task_columns[2].metric(
                "Consolidated correction",
                str(diagnostics.get("consolidated_correction_status", "not_needed")),
            )
            task_columns[3].metric(
                "Corrected-claim verification",
                str(
                    diagnostics.get(
                        "corrected_claim_verification_status", "not_needed"
                    )
                ),
            )
            batch_columns = st.columns(3)
            batch_columns[0].metric(
                "Corrected fields",
                str(diagnostics.get("consolidated_correction_field_count", 0)),
            )
            batch_columns[1].metric(
                "Failed draft batches",
                str(diagnostics.get("draft_batches_failed", 0)),
            )
            batch_columns[2].metric(
                "Failed semantic reviews",
                str(diagnostics.get("semantic_reviews_failed", 0)),
            )
            prompt_columns = st.columns(5)
            prompt_columns[0].metric(
                "Largest market prompt",
                (
                    f"{int(diagnostics.get('market_draft_max_observed_prompt_characters', 0) or 0):,} "
                    "characters"
                ),
            )
            prompt_columns[1].metric(
                "Market prompt limit",
                (
                    f"{int(diagnostics.get('market_draft_prompt_max_characters', 0) or 0):,} "
                    "characters"
                ),
            )
            prompt_columns[2].metric(
                "Market call deadline",
                (
                    f"{float(diagnostics.get('market_draft_timeout_seconds', 0) or 0):g} "
                    "seconds"
                ),
            )
            semantic_prompt_sizes = [
                int(item.get("prompt_character_count") or 0)
                for item in diagnostics.get("semantic_reviews", []) or []
                if isinstance(item, dict)
            ]
            prompt_columns[3].metric(
                "Largest semantic-review prompt",
                f"{max(semantic_prompt_sizes, default=0):,} characters",
            )
            prompt_columns[4].metric(
                "Semantic-review prompt limit",
                (
                    f"{int(diagnostics.get('red_team_package_target_characters', 0) or 0):,} "
                    "characters"
                ),
            )
        st.json(result.get("generation_diagnostics") or {})
        st.markdown("**LLM call diagnostics**")
        render_llm_diagnostics(result.get("llm_diagnostics") or {})
        st.markdown("**Context status**")
        context_label = str(context_status.get("status") or "not_attempted").replace(
            "_", " "
        )
        context_message = f"Context evidence status: {context_label}."
        if context_status.get("limitation_code"):
            st.warning(
                f"{context_message} Limitation code: "
                f"{context_status['limitation_code']}."
            )
        else:
            st.info(context_message)
        st.json(context_status)
        if result.get("warnings"):
            st.markdown("**Generation notices**")
            for warning in result.get("warnings") or []:
                st.warning(str(warning))
        render_mfi_raw_table_downloads(
            assessment_profile,
            key_prefix=f"mfi_{display_run_id or 'completed'}",
        )
        st.markdown("**Deterministic assessment profile**")
        st.json(assessment_profile)
        if not is_light:
            st.markdown("**Claim validation**")
            st.json(result.get("claim_validation") or {})
            st.markdown("**QA review**")
            st.json(qa_review)

    if run_id:
        render_report_delivery(
            run_id=str(run_id),
            key_prefix="mfi",
            export_path=f"/mfi-drafter/export-docx/{run_id}",
            file_name=f"mfi-drafter-{run_id}.docx",
            render_preview=_preview,
            render_technical_details=_technical_details,
        )
