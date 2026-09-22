from pathlib import Path
from typing import Optional

import streamlit as st

from streamlit_shared import (
    apply_wfp_theme,
    render_bug_report_sidebar_link,
    render_instructions_sidebar_button,
    render_onboarding_sidebar_button,
    render_wfp_sidebar_logo,
)

st.set_page_config(page_title="How to use the tools", layout="wide")
apply_wfp_theme()

with st.sidebar:
    render_wfp_sidebar_logo()
    render_onboarding_sidebar_button(key="sidebar_onboarding_instructions")
    render_instructions_sidebar_button(key="sidebar_instructions_page")
    render_bug_report_sidebar_link()


st.markdown(
    """
    <style>
    .jump-links {
        background: rgba(255, 255, 255, 0.7);
        border: 1px solid rgba(0, 58, 93, 0.12);
        border-radius: 16px;
        padding: 1rem 1.25rem;
        box-shadow: 0 10px 24px rgba(0, 58, 93, 0.08);
        margin: 1.5rem 0 2rem 0;
    }
    .jump-links ul {
        list-style: none;
        padding-left: 0;
        margin: 0;
    }
    .jump-links li {
        margin: 0.35rem 0;
    }
    .jump-links a {
        text-decoration: none;
        color: var(--wfp-primary);
        font-weight: 600;
    }
    .jump-links a:hover {
        text-decoration: underline;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("VAM LLM — Service Instructions")

st.markdown(
    """
## About this application

This application provides three AI-powered **report drafters** for WFP food security analysts. They generate complete analytical reports from WFP data, automating work that currently requires significant manual effort while maintaining the analytical standards expected in WFP publications.

- **Price Bulletin Drafter** — a Market Price Bulletin for a country and month, from the DataBridges price series.
- **MFI Report Drafter** — a Market Functionality Index assessment from a processed MFI dataset.
- **Seasonal Outlook Drafter** — a Seasonal Outlook report from the climate forecast maps you upload, with an analyst review step before drafting.

**Looking for the dataset validators?** The MFI Dataset Validator and the Price Data Validator are no longer part of this application. They live in **MarketAIssist**, a separate app dedicated to cleaning and validating datasets before they are uploaded to DataBridges. Use MarketAIssist first, this app afterwards.

This application is currently in **ALPHA** (an early internal testing phase with a limited number of users). The tools you see here are prototypes undergoing active testing and refinement. Your feedback during this phase is essential to improve their accuracy, usability, and relevance to real-world workflows.
    """
)

st.markdown("## Jump to a tool")
st.markdown(
    """
    <div class="jump-links">
        <ul>
            <li><a href="#price-bulletin-drafter">Price Bulletin Drafter</a></li>
            <li><a href="#mfi-report-drafter">MFI Report Drafter</a></li>
            <li><a href="#seasonal-outlook-drafter">Seasonal Outlook Drafter</a></li>
        </ul>
    </div>
    """,
    unsafe_allow_html=True,
)


def _asset_path(filename: str) -> Optional[Path]:
    root_dir = Path(__file__).resolve().parent.parent
    candidate = root_dir / "app" / "shared" / "assets" / filename
    return candidate if candidate.exists() else None


def _render_tool_image(filename: str, caption: str) -> None:
    image_path = _asset_path(filename)
    if image_path is None:
        st.info(f"Screenshot not found: {filename}")
        return
    st.image(str(image_path), caption=caption, width="stretch")


st.markdown("---")

st.markdown('<a id="price-bulletin-drafter"></a>', unsafe_allow_html=True)
st.header("1. Price Bulletin Drafter")

st.subheader("What it does")
st.markdown(
    """
The Price Bulletin Drafter generates a complete Market Price Bulletin report for a given country and month. It analyzes price changes for the selected period by comparing them against the previous 12 months of data, and enriches the analysis with contextual information from recent news and reports.

Each country uses a required **primary food basket** and may also configure an optional **second basket**. The baskets retain separate names, descriptions, quantities, geographic scopes, calculations, charts, and narrative facts.
    """
)

st.subheader("How to use it")
left, right = st.columns([1.35, 1])
with left:
    st.markdown(
        """
1. **Select a country.** The application loads the available commodities, units, regions, and reportable months.
2. **Configure the primary basket.** A blank primary name becomes `MEB`. Custom primary names require a description. Choose national or selected-region scope and publish at least one positive commodity quantity.
3. **Optionally add a second basket.** It requires its own name, description, scope, regions where applicable, and positive quantities. Editing publishes a new immutable version. Removing it archives the active version without deleting report history.
4. **Choose whether to include the second basket.** Every new report iteration defaults to included. Uncheck it for a primary-only run without changing the saved configuration.
5. **Select report regions.** Run regions remain independent from basket scope. A selected-region basket must overlap the run regions; an empty region selection means all available regions.
6. **Select a reportable month.** Eligibility requires complete prices for every included basket component. Including a second basket may move the latest jointly reportable month back. Use **Refresh from DataBridges** to attempt a selection-aware refresh.
7. **Review locked and additional commodities.** Basket components are locked by commodity ID. You may add other priced commodities; shared components are retrieved once but retain their basket-specific quantities.
8. **Choose the report language and optional analysis modules.** English, French, and Spanish are supported. Exchange-rate, fuel, livestock, and labour modules can be enabled when relevant data is available. News dates are selected automatically from the report month.
9. **Click "Run".** The submitted primary and optional secondary version IDs are recorded immutably for that run.
        """
    )
with right:
    _render_tool_image("price_bulletin_drafter.jpeg", "Price Bulletin Drafter")

st.subheader("Output")
st.markdown(
    """
The agent takes approximately **10 minutes** to complete. When the report is ready, the first action shown is **Download report (.docx)**. Use **View report on this page** underneath it only when you want to review the report in Streamlit without downloading it. Run information, QA notices, and supporting statistics remain available in the closed **Technical details** section. Historical runs continue using their captured basket versions even after a basket is edited or archived.
    """
)

st.markdown("---")

st.markdown('<a id="mfi-report-drafter"></a>', unsafe_allow_html=True)
st.header("2. MFI Report Drafter")

st.subheader("What it does")
st.markdown(
    """
The MFI Report Drafter generates a complete Market Functionality Index (MFI) assessment report from processed MFI data uploaded as a final processed/elaborated CSV.
    """
)

st.subheader("When to use it")
st.markdown(
    """
Use this tool **after** your raw MFI data has been processed by DataBridges. Upload the final processed CSV with valid `Adm0Name`, `StartDate`, and `EndDate` metadata. If required metadata is missing or invalid, the application will ask you to correct the CSV before generating a report.
    """
)

st.subheader("How to use it")
left, right = st.columns([1.35, 1])
with left:
    st.markdown(
        """
1. **Upload the final processed MFI CSV.**
2. **Confirm the file contains valid country and collection-date metadata.** Missing or invalid fields are listed in a pop-up before generation starts.
3. **Click "Generate report"** to create the MFI report.
        """
    )
with right:
    _render_tool_image("mfi_report_drafter.jpeg", "MFI Report Drafter")

st.subheader("Output")
st.markdown(
    """
The agent takes approximately **20 minutes** to complete. When it is ready, use the large **Download report (.docx)** button first. To read it without downloading, click **View report on this page** underneath. The report preview stays closed until requested.
    """
)
st.markdown("---")

st.markdown('<a id="seasonal-outlook-drafter"></a>', unsafe_allow_html=True)
st.header("3. Seasonal Outlook Drafter")

st.subheader("What it does")
st.markdown(
    """
The Seasonal Outlook Drafter turns the seasonal climate forecast maps you already consult into a drafted Seasonal Outlook report for a region and report date. It reads the maps, extracts the evidence they contain, **pauses for your review**, and only drafts the report once you have confirmed that evidence.

The analyst review in the middle is the point of the tool: the model never publishes a claim you have not seen. Reviewing evidence and comparing it with the original maps is part of the workflow, not an optional check.
    """
)

st.subheader("When to use it")
st.markdown(
    """
Use it when you have the forecast maps for the season and you want a first draft that is traceable to them. It does not fetch forecasts itself: whatever you upload is the entire evidence base. Corrections to the region, the report date or the maps themselves require a new analysis — inputs freeze once extraction starts.
    """
)

st.subheader("How to use it")
st.markdown(
    """
1. **Select a region and a report date**, then start an input package. The analysis gets its own URL that you and your colleagues can reopen.
2. **Upload the maps** in the *Input package* tab and press **Save selected maps**. Between 1 and 12 static PNG, JPEG or WebP images; at most 30 MB per file and 50 MB in total. Category, issue date and notes are optional — unclassified maps are identified during extraction. The product checklist next to the uploader shows what the seasonal calendar expects for that region and date.
3. **Click "Extract and review evidence".** Processing continues in the cloud even if you close the page; reopen the URL later.
4. **Review the extracted evidence** in the *Evidence and analyst review* tab, side by side with the original map it came from. Compare versions if you have asked for revisions before.
5. **Either revise or confirm.** Writing comments and clicking **Revise evidence** produces a new evidence version and another pause. **Confirm evidence and draft report** locks the version you are looking at and starts the drafting phases.
6. **Collect the report** in the *Report and downloads* tab.
    """
)

st.subheader("Output")
st.markdown(
    """
A Seasonal Outlook report, downloadable as Word with or without the map appendix, plus an artifact ZIP containing the scientific rules, the frozen inputs, the original images byte-for-byte, every evidence version, your review decisions and the model requests and responses — enough to reconstruct how any sentence came to be.

Each phase can take several minutes, and a full run has two waits: extraction, then drafting after your confirmation. Earlier reports stay available under their own operation even after new feedback supersedes them.
    """
)
