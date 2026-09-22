import streamlit as st

from streamlit_shared import (
    INSTRUCTIONS_PAGE_PATH,
    ONBOARDING_PAGE_PATH,
    apply_wfp_theme,
    render_bug_report_sidebar_link,
    render_instructions_sidebar_button,
    render_onboarding_sidebar_button,
    render_wfp_sidebar_logo,
)

st.set_page_config(page_title="VAM LLM Testing App", layout="wide")
apply_wfp_theme()

with st.sidebar:
    st.markdown("## VAM LLM Testing App")
    render_wfp_sidebar_logo()
    render_onboarding_sidebar_button(key="sidebar_onboarding_home")
    render_instructions_sidebar_button(key="sidebar_instructions_home")
    render_bug_report_sidebar_link()

st.title("VAM LLM Testing App")


def _go_to(page_path: str) -> None:
    if hasattr(st, "switch_page"):
        st.switch_page(page_path)
        return
    st.info("Use the pages in the left sidebar to access each service.")


st.markdown(
    """
    <style>
    .landing-menu {
        max-width: 760px;
        margin: 2.5rem auto 0 auto;
    }
    .landing-menu [data-testid="stMarkdownContainer"] h3 {
        text-align: center;
        margin-bottom: 1.5rem;
    }
    .onboarding-cta {
        max-width: 720px;
        margin: 0.5rem auto 0 auto;
        text-align: center;
    }
    .onboarding-cta .stButton > button,
    .onboarding-cta div[data-testid^="baseButton-"] button {
        width: 100% !important;
        height: 84px !important;
        font-size: 1.08rem !important;
        margin-top: 0.5rem !important;
    }
    .landing-menu .stButton > button,
    .landing-menu div[data-testid^="baseButton-"] button,
    .landing-menu div[data-testid="stFormSubmitButton"] button,
    .landing-menu .stDownloadButton > button {
        width: 100% !important;
        height: 78px !important;
        font-size: 1.05rem !important;
        padding: 0.85rem 1.1rem !important;
        justify-content: center !important;
        margin-bottom: 1rem !important;
    }
    .instructions-menu {
        max-width: 520px;
        margin: 1.5rem auto 0 auto;
    }
    .instructions-menu .stButton > button,
    .instructions-menu div[data-testid^="baseButton-"] button {
        width: 100% !important;
        height: 62px !important;
        font-size: 0.98rem !important;
        background: #FFFFFF !important;
        color: var(--wfp-primary) !important;
        border: 2px solid var(--wfp-primary) !important;
        border-radius: 999px !important;
        margin-bottom: 0.5rem !important;
    }
    .instructions-menu .stButton > button:hover,
    .instructions-menu div[data-testid^="baseButton-"] button:hover {
        background: var(--wfp-light) !important;
        color: var(--wfp-primary-dark) !important;
        border-color: var(--wfp-primary-dark) !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="onboarding-cta">', unsafe_allow_html=True)
st.markdown("### Tester Onboarding")
st.markdown("Start here before using the tools.")
if st.button(
    "Tester Onboarding",
    key="nav_onboarding",
    use_container_width=True,
):
    _go_to(ONBOARDING_PAGE_PATH)
st.markdown("</div>", unsafe_allow_html=True)

st.markdown('<div class="landing-menu">', unsafe_allow_html=True)

st.markdown("### Select a service")
left, right = st.columns(2)

with left:
    if st.button(
        "Price Bulletin Drafter",
        type="primary",
        key="nav_price_bulletin",
        use_container_width=True,
    ):
        _go_to("pages/3_Price_Bulletin_Drafter.py")

with right:
    if st.button(
        "MFI Report Generator",
        type="primary",
        key="nav_mfi_drafter",
        use_container_width=True,
    ):
        _go_to("pages/4_MFI_Drafter.py")

if st.button("Seasonal Outlook Drafter", type="primary", key="nav_seasonal_outlook", use_container_width=True):
    _go_to("pages/5_Seasonal_Outlook_Drafter.py")

st.markdown("</div>", unsafe_allow_html=True)

st.markdown("---")
st.markdown('<div class="instructions-menu">', unsafe_allow_html=True)
st.markdown("#### Need guidance?")
if st.button(
    "How to use the tools",
    key="nav_instructions",
    use_container_width=True,
):
    _go_to(INSTRUCTIONS_PAGE_PATH)
st.markdown("</div>", unsafe_allow_html=True)
