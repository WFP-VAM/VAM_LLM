import streamlit as st

from streamlit_shared import (
    apply_wfp_theme,
    render_bug_report_header_link,
    render_bug_report_sidebar_link,
    render_instructions_sidebar_button,
    render_onboarding_sidebar_button,
    quote_path_param,
    render_llm_diagnostics,
    render_report_delivery,
    render_report_blocks,
    render_wfp_sidebar_logo,
    request_json,
    run_async_and_poll,
    safe_show_error,
)
from app.services.market_monitor.basket_ui import (
    DEFAULT_PRIMARY_NAME,
    NATIONAL_SCOPE,
    PRIMARY_ROLE,
    SECONDARY_ROLE,
    SELECTED_REGIONS_SCOPE,
    BasketUIValidationError,
    additional_commodity_ids,
    advance_report_iteration,
    basket_item_name,
    basket_items,
    build_basket_save_payload,
    build_editor_rows,
    clear_role_state,
    clear_secondary_inclusion_state,
    commodity_catalog,
    inclusion_widget_key,
    locked_commodity_ids,
    role_state_prefix,
    run_commodity_names,
    sanitize_selected_commodity_ids,
    scope_overlap_errors,
    sync_report_iteration_context,
    unavailable_basket_regions,
)
from app.services.market_monitor.i18n import LANGUAGE_NAMES, t


def _clear_cache_version_dependent_state():
    for key in (
        "mm_countries_resp",
        "mm_countries_resp_version",
        "mm_country_metadata",
        "mm_country_basket",
        "mm_country_baskets",
        "mm_country_reportable_months",
        "mm_manual_refresh_result",
    ):
        st.session_state.pop(key, None)


def _basket_version_id(basket):
    if not isinstance(basket, dict):
        return ""
    return str(basket.get("basket_version_id") or "")


def _basket_label(role):
    return "Primary basket" if role == PRIMARY_ROLE else "Second basket"


def _render_basket_summary(role, basket):
    items = basket_items(basket)
    scope = str(basket.get("scope_type") or NATIONAL_SCOPE)
    scope_label = "National"
    if scope == SELECTED_REGIONS_SCOPE:
        scope_regions = [str(region) for region in basket.get("regions") or [] if str(region).strip()]
        scope_label = "Selected regions: " + (", ".join(scope_regions) or "none")

    with st.container(border=True):
        st.markdown(f"**{basket.get('basket_name') or _basket_label(role)}**")
        if basket.get("short_description"):
            st.write(str(basket.get("short_description")))
        st.caption(scope_label)
        columns = st.columns(2)
        columns[0].metric("Version", str(basket.get("version_number") or "n/a"))
        columns[1].metric("Items", str(len(items)))
        st.caption(f"Published: {basket.get('created_at') or 'n/a'}")
        if basket.get("change_note"):
            st.caption(f"Change note: {basket.get('change_note')}")
        summary_rows = [
            {
                "Commodity": basket_item_name(item),
                "Quantity": item.get("weight_quantity"),
                "Unit": item.get("databridges_unit") or item.get("unit"),
                "Note": item.get("item_note") or "",
            }
            for item in items
        ]
        if summary_rows:
            st.dataframe(summary_rows, hide_index=True, width="stretch")


def _render_basket_editor(*, role, country, basket, raw_commodities, regions):
    """Render one role-neutral editor and publish through the plural endpoint."""
    label = _basket_label(role)
    version_token = _basket_version_id(basket) or "new"
    prefix = f"{role_state_prefix(country, role)}{version_token}_"
    is_primary = role == PRIMARY_ROLE

    with st.container(border=True):
        st.caption("Saved basket versions are shared across users and publish immediately.")
        basket_name = st.text_input(
            "Basket name",
            value=str(basket.get("basket_name") or "") if basket else (DEFAULT_PRIMARY_NAME if is_primary else ""),
            help="Minimum Expenditure Basket" if is_primary else None,
            key=f"{prefix}name",
        )
        short_description = st.text_area(
            "Short description",
            value=str(basket.get("short_description") or "") if basket else "",
            height=80,
            placeholder=(
                "Optional for MEB; required for a custom primary name."
                if is_primary
                else "For example: affordability proxy, emergency ration, healthy-diet proxy, urban basket."
            ),
            key=f"{prefix}description",
        )
        initial_scope = str(basket.get("scope_type") or NATIONAL_SCOPE) if basket else NATIONAL_SCOPE
        scope_type = st.radio(
            "Geographic scope",
            options=[NATIONAL_SCOPE, SELECTED_REGIONS_SCOPE],
            index=1 if initial_scope == SELECTED_REGIONS_SCOPE else 0,
            format_func=lambda value: "National" if value == NATIONAL_SCOPE else "Selected regions",
            horizontal=True,
            key=f"{prefix}scope",
        )

        available_regions = [str(region) for region in regions or [] if str(region).strip()]
        available_region_keys = {region.casefold(): region for region in available_regions}
        stale_regions = unavailable_basket_regions(basket, available_regions)
        saved_regions = [
            available_region_keys[str(region).strip().casefold()]
            for region in (basket.get("regions") or [] if basket else [])
            if str(region).strip().casefold() in available_region_keys
        ]
        saved_regions.extend(stale_regions)
        region_options = available_regions + [
            region for region in stale_regions if region.casefold() not in available_region_keys
        ]
        selected_regions = st.multiselect(
            "Basket regions",
            options=region_options,
            default=saved_regions,
            format_func=lambda region: (
                region if region.casefold() in available_region_keys else f"{region} (unavailable)"
            ),
            disabled=scope_type != SELECTED_REGIONS_SCOPE,
            key=f"{prefix}regions",
        )
        if stale_regions:
            st.warning(
                "These saved regions are no longer available in the active cache and cannot be republished: "
                + ", ".join(stale_regions)
            )

        editor_rows = build_editor_rows(raw_commodities, basket)
        if any(row.get("Available") is False for row in editor_rows):
            st.warning(
                "Unavailable saved commodities remain visible below. Uncheck and replace them before publishing."
            )
        column_config = {}
        if hasattr(st, "column_config"):
            column_config = {
                "Include": st.column_config.CheckboxColumn("Include"),
                "Available": st.column_config.CheckboxColumn("Available"),
                "Quantity": st.column_config.NumberColumn(
                    "Quantity", min_value=0.0, step=0.01, format="%.2f"
                ),
                "Note": st.column_config.TextColumn("Note"),
            }
        edited_rows = st.data_editor(
            editor_rows,
            hide_index=True,
            width="stretch",
            num_rows="fixed",
            disabled=["Available", "Commodity ID", "Commodity", "Unit"],
            column_config=column_config,
            key=f"{prefix}items",
        )
        change_note = st.text_input(
            "Change note",
            value="",
            placeholder="Optional note for this published basket version",
            key=f"{prefix}change_note",
        )

        button_columns = st.columns(2)
        save_clicked = button_columns[0].button(
            f"Publish {label.lower()}",
            type="primary",
            key=f"{prefix}save",
        )
        cancel_clicked = False
        if basket or not is_primary:
            cancel_clicked = button_columns[1].button("Cancel", key=f"{prefix}cancel")

    if cancel_clicked:
        clear_role_state(st.session_state, country, role)
        st.rerun()

    if not save_clicked:
        return

    try:
        payload = build_basket_save_payload(
            role=role,
            basket_name=basket_name,
            short_description=short_description,
            scope_type=scope_type,
            selected_regions=selected_regions,
            available_regions=available_regions,
            edited_rows=edited_rows,
            change_note=change_note,
            created_by_user_id="streamlit",
        )
    except BasketUIValidationError as exc:
        for error in exc.errors:
            st.error(error)
        return

    try:
        request_json(
            "POST",
            f"/market-monitor/countries/{quote_path_param(country)}/baskets/{role}",
            json_body=payload,
            timeout=60,
        )
        clear_role_state(st.session_state, country, role)
        if role == PRIMARY_ROLE:
            st.session_state.pop("mm_country_reportable_months", None)
        else:
            clear_secondary_inclusion_state(st.session_state, country)
        st.session_state["mm_basket_flash"] = f"{label} published."
        st.rerun()
    except Exception as exc:
        safe_show_error(exc)


def _render_basket_card(*, role, country, basket, raw_commodities, regions):
    label = _basket_label(role)
    badge = "Required" if role == PRIMARY_ROLE else "Optional"
    st.markdown(f"#### {label} - {badge}")

    editing_key = f"{role_state_prefix(country, role)}editing"
    if basket:
        _render_basket_summary(role, basket)
        action_columns = st.columns([1, 1, 3])
        if action_columns[0].button(f"Edit {label.lower()}", key=f"{editing_key}_open"):
            st.session_state[editing_key] = True
            st.rerun()

        if role == SECONDARY_ROLE:
            confirm_key = f"{role_state_prefix(country, role)}confirm_archive"
            if action_columns[1].button("Remove second basket", key=f"{confirm_key}_open"):
                st.session_state[confirm_key] = True
                st.rerun()
            if st.session_state.get(confirm_key):
                st.warning(
                    "Remove the active second basket? Its immutable versions and history will remain available."
                )
                confirm_columns = st.columns(2)
                if confirm_columns[0].button(
                    "Confirm removal", type="primary", key=f"{confirm_key}_yes"
                ):
                    try:
                        request_json(
                            "DELETE",
                            f"/market-monitor/countries/{quote_path_param(country)}/baskets/secondary",
                            timeout=60,
                        )
                        clear_role_state(st.session_state, country, SECONDARY_ROLE)
                        clear_secondary_inclusion_state(st.session_state, country)
                        st.session_state["mm_basket_flash"] = (
                            "Second basket removed. Historical versions were preserved."
                        )
                        st.rerun()
                    except Exception as exc:
                        safe_show_error(exc)
                if confirm_columns[1].button("Cancel", key=f"{confirm_key}_no"):
                    st.session_state.pop(confirm_key, None)
                    st.rerun()
    elif role == PRIMARY_ROLE:
        st.warning("No primary food basket exists for this country. Publish one before generating a bulletin.")
        st.session_state[editing_key] = True
    else:
        st.info("No second basket is configured for this country.")
        if st.button("Add second basket", key=f"{editing_key}_add"):
            st.session_state[editing_key] = True
            st.rerun()

    if st.session_state.get(editing_key):
        _render_basket_editor(
            role=role,
            country=country,
            basket=basket,
            raw_commodities=raw_commodities,
            regions=regions,
        )


st.set_page_config(page_title="Price Bulletin Drafter", layout="wide")
apply_wfp_theme()

with st.sidebar:
    render_wfp_sidebar_logo()
    render_onboarding_sidebar_button(key="sidebar_onboarding_price_bulletin")
    render_instructions_sidebar_button(key="sidebar_instructions_price_bulletin")
    render_bug_report_sidebar_link()

title_col, bug_col = st.columns([3, 1])
with title_col:
    st.title("Price Bulletin Drafter")
with bug_col:
    render_bug_report_header_link()
st.subheader("Generate Report")

countries = []
country_currency = {}
cache_status = {}

cache_status_resp = None
try:
    cache_status_resp = request_json("GET", "/market-monitor/cache/status", timeout=30)
    current_cache_version = (
        cache_status_resp.get("active_version_id")
        if isinstance(cache_status_resp, dict)
        else None
    )
    previous_cache_version = st.session_state.get("mm_cache_status_version")
    if (
        current_cache_version
        and previous_cache_version
        and current_cache_version != previous_cache_version
    ):
        _clear_cache_version_dependent_state()
    st.session_state["mm_cache_status_resp"] = cache_status_resp
    st.session_state["mm_cache_status_version"] = current_cache_version
except Exception:
    cache_status_resp = st.session_state.get("mm_cache_status_resp")
    if cache_status_resp is None:
        st.session_state.pop("mm_cache_status_resp", None)

if isinstance(cache_status_resp, dict):
    cache_status = cache_status_resp

active_version = str(cache_status.get("active_version_id") or "")

if not cache_status.get("has_active_cache"):
    st.warning("No price data available.")
    st.stop()

countries_resp = (
    st.session_state.get("mm_countries_resp")
    if st.session_state.get("mm_countries_resp_version") == active_version
    else None
)
if countries_resp is None:
    try:
        countries_resp = request_json("GET", "/market-monitor/countries", timeout=30)
        st.session_state["mm_countries_resp"] = countries_resp
        st.session_state["mm_countries_resp_version"] = active_version
    except Exception:
        countries_resp = None
        st.session_state.pop("mm_countries_resp", None)

if isinstance(countries_resp, dict):
    if not cache_status and isinstance(countries_resp.get("cache_status"), dict):
        cache_status = countries_resp["cache_status"]
    countries_list = countries_resp.get("countries") or []
    if isinstance(countries_list, list):
        for c in countries_list:
            if isinstance(c, dict) and c.get("has_data"):
                name = c.get("name")
                if isinstance(name, str):
                    countries.append(name)
                    country_currency[name] = c.get("currency_code")

if countries:
    country = st.selectbox("Country", countries, index=0, key="mm_country")
else:
    st.warning("No countries or commodities available.")
    st.stop()

metadata = None
regions = []
commodities = []
default_commodities = []
time_period_options = []
default_time_period = None

if country:
    metadata_cache = st.session_state.setdefault("mm_country_metadata", {})
    metadata_cache_key = (active_version, country)
    metadata = metadata_cache.get(metadata_cache_key)
    if metadata is None:
        try:
            metadata = request_json(
                "GET",
                f"/market-monitor/countries/{quote_path_param(country)}/metadata",
                timeout=30,
            )
            metadata_cache[metadata_cache_key] = metadata
        except Exception:
            metadata = None

if not isinstance(metadata, dict):
    st.warning("No countries or commodities available.")
    st.stop()

regions = metadata.get("regions") or []
raw_commodities = metadata.get("commodities") or []
commodities = [
    item.get("name")
    for item in raw_commodities
    if isinstance(item, dict) and isinstance(item.get("name"), str)
]
if not commodities:
    commodities = [item for item in raw_commodities if isinstance(item, str)]
default_commodities = metadata.get("default_commodities") or []

selected_version = str(metadata.get("cache_version_id") or "")

if not commodities:
    st.warning("No countries or commodities available.")
    st.stop()

baskets_resp = None
if country:
    try:
        baskets_resp = request_json(
            "GET",
            f"/market-monitor/countries/{quote_path_param(country)}/baskets",
            timeout=30,
        )
    except Exception:
        baskets_resp = None

if not isinstance(baskets_resp, dict):
    st.warning("Basket configuration is not available for the selected country.")
    st.stop()

active_primary = baskets_resp.get(PRIMARY_ROLE)
if not isinstance(active_primary, dict):
    active_primary = None
active_secondary = baskets_resp.get(SECONDARY_ROLE)
if not isinstance(active_secondary, dict):
    active_secondary = None
second_basket_enabled = bool(baskets_resp.get("second_basket_enabled", True))
if not second_basket_enabled:
    clear_role_state(st.session_state, country, SECONDARY_ROLE)
    clear_secondary_inclusion_state(st.session_state, country)
    active_secondary = None

flash_message = st.session_state.pop("mm_basket_flash", None)
if flash_message:
    st.success(str(flash_message))

_render_basket_card(
    role=PRIMARY_ROLE,
    country=country,
    basket=active_primary,
    raw_commodities=raw_commodities,
    regions=regions,
)
if second_basket_enabled:
    _render_basket_card(
        role=SECONDARY_ROLE,
        country=country,
        basket=active_secondary,
        raw_commodities=raw_commodities,
        regions=regions,
    )
else:
    st.info("Second-basket configuration and selection are temporarily disabled. Primary-only reports remain available.")

secondary_version_id = _basket_version_id(active_secondary)
iteration = sync_report_iteration_context(
    st.session_state,
    country=country,
    secondary_version_id=secondary_version_id,
)
include_secondary_basket = False
if active_secondary:
    include_secondary_basket = st.checkbox(
        f"Include {active_secondary.get('basket_name') or 'second basket'} in this report",
        value=True,
        key=inclusion_widget_key(country, secondary_version_id, iteration),
        help="This is a run-level choice and does not change the saved basket.",
    )
    if include_secondary_basket:
        st.info(
            "The second basket is calculated independently and recorded with immutable coverage statistics. "
            "Scope-specific second-basket charts are included; basket-aware narrative checks keep roles and scopes "
            "distinct without directly comparing absolute costs."
        )

admin1_list = st.multiselect(
    "Regions (Admin1)",
    options=[region for region in regions if isinstance(region, str)],
    default=[region for region in regions if isinstance(region, str)],
    key=f"mm_regions_{country}" if isinstance(country, str) and country else "mm_regions",
    help="Run regions are independent from each basket's configured scope and affect regional-basket eligibility.",
)

reportability_selection = {
    "primary_basket_version_id": active_primary.get("basket_version_id") if active_primary else None,
    "include_secondary_basket": bool(include_secondary_basket),
    "secondary_basket_version_id": (
        active_secondary.get("basket_version_id")
        if include_secondary_basket and active_secondary
        else None
    ),
    "admin1_list": list(admin1_list),
}

reportable_resp = None
reportable_cache_key = None
reportable_fetch_error = None
if active_primary:
    reportable_cache = st.session_state.setdefault("mm_country_reportable_months", {})
    reportable_cache_key = (
        selected_version,
        country,
        active_primary.get("basket_version_id"),
        active_secondary.get("basket_version_id") if include_secondary_basket and active_secondary else "",
        bool(include_secondary_basket),
        tuple(admin1_list),
    )
    reportable_resp = reportable_cache.get(reportable_cache_key)
    if reportable_resp is None:
        try:
            reportable_resp = request_json(
                "GET",
                f"/market-monitor/countries/{quote_path_param(country)}/reportable-months",
                params=reportability_selection,
                timeout=180,
            )
            reportable_cache[reportable_cache_key] = reportable_resp
        except Exception as exc:
            reportable_resp = None
            reportable_fetch_error = str(exc)

if isinstance(reportable_resp, dict):
    raw_months = reportable_resp.get("reportable_months") or []
    time_period_options = [str(item) for item in raw_months if isinstance(item, str)]
    default_time_period = reportable_resp.get("latest_reportable_month")
    latest_cached_month = reportable_resp.get("latest_cached_month")
    latest_reportable_month = reportable_resp.get("latest_reportable_month")
    latest_primary_reportable = reportable_resp.get("latest_primary_reportable_month")
    latest_joint_reportable = reportable_resp.get("latest_joint_reportable_month")
    if (
        include_secondary_basket
        and active_secondary
        and latest_primary_reportable
        and (
            not latest_joint_reportable
            or str(latest_joint_reportable) < str(latest_primary_reportable)
        )
    ):
        if latest_joint_reportable:
            rollback_target = str(latest_joint_reportable)
        else:
            rollback_target = "no jointly reportable month"
        st.info(
            "The selected basket has incomplete prices for newer months. "
            f"Including {active_secondary.get('basket_name') or 'the second basket'} changes the latest reportable "
            f"month from {latest_primary_reportable} to {rollback_target}."
        )
    if latest_cached_month and latest_reportable_month and str(latest_cached_month) > str(latest_reportable_month):
        st.info(
            "The selected basket has incomplete prices for newer months. "
            f"The latest reportable month is {latest_reportable_month}; the latest available price month is "
            f"{latest_cached_month}."
        )

refresh_cols = st.columns([1, 3])
with refresh_cols[0]:
    refresh_clicked = st.button(
        "Refresh from DataBridges",
        key=f"mm_refresh_databridges_{country}" if isinstance(country, str) and country else "mm_refresh_databridges",
        disabled=not bool(active_primary),
    )
with refresh_cols[1]:
    if isinstance(reportable_resp, dict):
        st.caption(
            "Latest reportable month: "
            f"{reportable_resp.get('latest_reportable_month') or 'n/a'} | "
            f"Latest available price month: {reportable_resp.get('latest_cached_month') or 'n/a'}"
        )

if refresh_clicked and active_primary:
    try:
        refresh_result = request_json(
            "POST",
            f"/market-monitor/countries/{quote_path_param(country)}/reportable-months/refresh",
            json_body=reportability_selection,
            timeout=120,
        )
        _clear_cache_version_dependent_state()
        st.session_state["mm_manual_refresh_result"] = refresh_result
        reportable_resp = request_json(
            "GET",
            f"/market-monitor/countries/{quote_path_param(country)}/reportable-months",
            params=reportability_selection,
            timeout=180,
        )
        if isinstance(reportable_resp, dict):
            time_period_options = [
                str(item)
                for item in reportable_resp.get("reportable_months") or []
                if isinstance(item, str)
            ]
            default_time_period = reportable_resp.get("latest_reportable_month")
            if default_time_period in time_period_options:
                st.session_state[f"mm_time_period_{country}"] = default_time_period
        status = refresh_result.get("status") if isinstance(refresh_result, dict) else None
        if status == "updated":
            st.success(
                "DataBridges refresh succeeded: "
                f"{refresh_result.get('rows_saved', 0)} new price rows were added."
            )
        elif status == "no_update":
            st.info("DataBridges refresh succeeded but found nothing new.")
        else:
            st.warning("DataBridges refresh failed. Please try again later.")
    except Exception:
        st.warning("DataBridges refresh failed. Please try again later.")

if not active_primary:
    st.info("Report generation is disabled until the primary food basket is published.")
    st.stop()

if active_primary and not time_period_options:
    st.warning("No reportable month is available. Try refreshing from DataBridges or check the basket data coverage.")
    if isinstance(reportable_resp, dict):
        for reportability_warning in reportable_resp.get("warnings") or []:
            if str(reportability_warning).strip():
                st.warning(str(reportability_warning))
        missing_by_month = reportable_resp.get("missing_by_month")
        if isinstance(missing_by_month, dict) and missing_by_month:
            latest_missing_month = max(missing_by_month)
            missing_names = [str(name) for name in missing_by_month.get(latest_missing_month) or []]
            if missing_names:
                shown_names = ", ".join(missing_names[:5])
                if len(missing_names) > 5:
                    shown_names += f" (+{len(missing_names) - 5} more)"
                st.caption(f"Missing in {latest_missing_month}: {shown_names}")
    else:
        st.caption(
            "Reportable-month details are unavailable because the data request failed or timed out. "
            "Reload the page to retry."
        )
        if reportable_fetch_error:
            st.caption(f"Details: {reportable_fetch_error}")
    st.stop()

with st.form("market_monitor_form"):
    time_period_index = 0
    if default_time_period in time_period_options:
        time_period_index = time_period_options.index(default_time_period)
    time_period_key = f"mm_time_period_{country}" if isinstance(country, str) and country else "mm_time_period"
    if st.session_state.get(time_period_key) not in (None, "") and st.session_state.get(time_period_key) not in time_period_options:
        st.session_state.pop(time_period_key, None)
    time_period = st.selectbox(
        "Time Period (YYYY-MM)",
        options=time_period_options,
        index=time_period_index,
        key=time_period_key,
    )

    language_options = ["auto", "en", "fr", "es"]
    language_labels = {
        code: t("en", f"ui.language.{code}")
        for code in language_options
    }
    selected_language_label = st.selectbox(
        t("en", "ui.language"),
        options=[language_labels[code] for code in language_options],
        index=0,
        key=f"mm_language_{country}" if isinstance(country, str) and country else "mm_language",
    )
    language = next(
        code for code, label in language_labels.items() if label == selected_language_label
    )

    included_baskets = [active_primary]
    if include_secondary_basket and active_secondary:
        included_baskets.append(active_secondary)
    catalog = commodity_catalog(raw_commodities)
    locked_ids = locked_commodity_ids(*included_baskets)
    additional_options = additional_commodity_ids(raw_commodities, locked_ids)
    default_name_keys = {
        str(name).strip().casefold()
        for name in default_commodities
        if isinstance(name, str) and str(name).strip()
    }
    default_additional_ids = [
        commodity_id
        for commodity_id in additional_options
        if str(catalog[commodity_id].get("name") or "").strip().casefold() in default_name_keys
    ]
    locked_basket_names = run_commodity_names(
        raw_commodities,
        included_baskets=included_baskets,
        additional_ids=[],
    )
    if locked_basket_names:
        st.caption("Included basket commodities are locked into this run: " + ", ".join(locked_basket_names))
    additional_key = (
        f"mm_additional_commodities_{country}"
        if isinstance(country, str) and country
        else "mm_additional_commodities"
    )
    if additional_key in st.session_state:
        st.session_state[additional_key] = sanitize_selected_commodity_ids(
            st.session_state.get(additional_key),
            additional_options,
        )
    additional_commodity_selection = st.multiselect(
        "Additional commodities",
        options=additional_options,
        default=default_additional_ids,
        format_func=lambda commodity_id: catalog.get(commodity_id, {}).get("name") or str(commodity_id),
        key=additional_key,
    )
    commodity_list = run_commodity_names(
        raw_commodities,
        included_baskets=included_baskets,
        additional_ids=additional_commodity_selection,
    )

    currency_default = (country_currency.get(country) or "USD") if isinstance(country, str) else "USD"
    currency_code = st.text_input("Currency Code", value=str(currency_default))

    enabled_modules = st.multiselect(
        "Enabled Modules",
        options=["exchange_rate", "fuel_energy", "livestock_animal_products", "labour_market"],
        default=[],
    )

    previous_report_text = st.text_area("Previous Report Text (optional)", value="", height=120)

    submitted = st.form_submit_button("Run", disabled=not bool(active_primary))

if submitted:
    overlap_errors = scope_overlap_errors(
        included_baskets,
        report_regions=admin1_list,
        available_regions=regions,
    )
    if overlap_errors:
        for error in overlap_errors:
            st.error(error)

if submitted and not overlap_errors:
    try:
        payload = {
            "country": country,
            "time_period": time_period,
            "language": language,
            "commodity_list": commodity_list,
            "admin1_list": admin1_list,
            "currency_code": currency_code,
            "enabled_modules": enabled_modules,
            "previous_report_text": previous_report_text or "",
            "use_mock_data": False,
            "primary_basket_version_id": active_primary.get("basket_version_id"),
            "include_secondary_basket": bool(include_secondary_basket),
            "secondary_basket_version_id": (
                active_secondary.get("basket_version_id")
                if include_secondary_basket and active_secondary
                else None
            ),
        }

        run_id, final_status, result = run_async_and_poll(
            start_method="POST",
            start_path="/market-monitor/generate-async",
            status_path_template="/market-monitor/status/{run_id}",
            result_path_template="/market-monitor/result/{run_id}",
            start_json=payload,
            poll_interval_seconds=2.0,
            timeout_seconds=3600,
        )
        st.session_state["mm_last_result"] = result
        st.session_state["mm_last_run_id"] = run_id
        for key in (
            "mm_docx_bytes",
            "mm_docx_run_id",
            "mm_docx_error",
            "mm_docx_error_run_id",
        ):
            st.session_state.pop(key, None)
        if isinstance(final_status, dict) and final_status.get("status") in {"completed", "failed"}:
            advance_report_iteration(st.session_state, country)
        st.rerun()

    except Exception as e:
        safe_show_error(e)

result = st.session_state.get("mm_last_result")
run_id = st.session_state.get("mm_last_run_id")

if isinstance(result, dict):

    display_run_id = str(run_id or result.get("run_id") or "")
    result_language = str(result.get("language") or "en")

    def _preview() -> None:
        render_report_blocks(result.get("report_blocks"), visualizations=result.get("visualizations"))

    def _technical_details() -> None:
        cols = st.columns(4)
        cols[0].metric(t(result_language, "ui.run_id"), display_run_id)
        cols[1].metric(t(result_language, "ui.country"), str(result.get("country") or ""))
        cols[2].metric(t(result_language, "ui.time_period"), str(result.get("time_period") or ""))
        cols[3].metric(t(result_language, "ui.llm_calls"), str(result.get("llm_calls") or 0))
        st.caption(
            t(
                result_language,
                "ui.resolved_language",
                language_name=LANGUAGE_NAMES.get(result_language, result_language),
            )
        )
        st.markdown("**LLM call diagnostics**")
        render_llm_diagnostics(result.get("llm_diagnostics") or {})

        result_warnings = result.get("warnings") or []
        if result_warnings:
            st.markdown(f"**{t(result_language, 'ui.warnings')}**")
            for warning in result_warnings:
                st.warning(str(warning))

        qa_review = result.get("qa_review") or {}
        qa_flags = qa_review.get("flags") or [] if isinstance(qa_review, dict) else []
        if qa_flags:
            st.markdown(f"**{t(result_language, 'ui.qa_review')}**")
            st.caption(
                t(
                    result_language,
                    "ui.qa_status",
                    status=str(qa_review.get("status") or "not_recorded"),
                    attempts=int(qa_review.get("correction_attempts") or 0),
                )
            )
            for flag in qa_flags:
                if not isinstance(flag, dict):
                    continue
                st.warning(
                    t(
                        result_language,
                        "ui.qa_flag",
                        section=str(flag.get("section") or "GLOBAL"),
                        severity=str(flag.get("severity") or "medium"),
                        details=str(flag.get("details") or flag.get("claim") or ""),
                    )
                )
        if result.get("data_statistics") is not None:
            st.markdown(f"**{t(result_language, 'ui.data_statistics')}**")
            st.json(result.get("data_statistics"))

    if run_id:
        render_report_delivery(
            run_id=str(run_id),
            key_prefix="mm",
            export_path=f"/market-monitor/export-docx/{run_id}",
            file_name=f"market-monitor-{run_id}.docx",
            render_preview=_preview,
            render_technical_details=_technical_details,
            labels={
                "ready": t(result_language, "ui.report_ready"),
                "ready_caption": t(result_language, "ui.report_ready_caption"),
                "preparing": t(result_language, "ui.preparing_docx"),
                "download": t(result_language, "ui.download_report"),
                "view": t(result_language, "ui.view_report"),
                "hide": t(result_language, "ui.hide_report"),
                "technical": t(result_language, "ui.technical_details"),
                "export_error": t(result_language, "ui.export_error"),
                "retry": t(result_language, "ui.retry_export"),
            },
        )
