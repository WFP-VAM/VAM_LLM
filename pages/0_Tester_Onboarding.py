import streamlit as st

from streamlit_shared import (
    BUG_REPORT_MESSAGE,
    INSTRUCTIONS_PAGE_URL,
    apply_wfp_theme,
    render_bug_report_sidebar_link,
    render_instructions_sidebar_button,
    render_onboarding_sidebar_button,
    render_wfp_sidebar_logo,
)

st.set_page_config(page_title="Tester Onboarding", layout="wide")
apply_wfp_theme()

with st.sidebar:
    render_wfp_sidebar_logo()
    render_onboarding_sidebar_button(key="sidebar_onboarding_page")
    render_instructions_sidebar_button(key="sidebar_instructions_onboarding")
    render_bug_report_sidebar_link()

st.markdown(
    f"""
# Tester Onboarding

---

## Welcome to the Alpha Test

Thank you for participating. Your feedback will directly shape how these tools are developed. You do not need to be technical or an AI expert — we need your judgment as a food security analyst.

Before you start, please read the **[Instructions page]({INSTRUCTIONS_PAGE_URL})** to understand what each tool does and how to use it.

This app drafts reports. Dataset validation before a DataBridges upload has
moved to a separate app, **MarketAIssist** — bug reports about the MFI Dataset
Validator or the Price Data Validator belong there, not here.

---

## What we ask you to do

Use the tools as part of your normal work. Whenever you would normally draft a Price Bulletin, an MFI report or a Seasonal Outlook, try using the corresponding drafter instead. Then tell us how it went.

There are **two ways** to give us feedback:

### 1. Bug Report — when something goes wrong

Every time the tool does not work as expected, submit a report through the **Bug Report form** (link at the bottom of this page). This includes situations where the tool crashes or shows an error, a number or classification in the output is wrong, the report contains information that contradicts the data or that does not exist in the sources, a section is missing or incomplete, the interface has visual or functional problems, or the exported .docx file has formatting issues.

Filling a bug report takes 3–5 minutes. The more specific you are, the more likely we are to identify and solve the problem. Here is what a useful report looks like:

> *"I generated an MFI report for Sudan (file: sudan_mfi_jan2025.csv, 450 rows). The executive summary says market functionality improved in Darfur, but the dimension table right below it shows a drop from 6.1 to 4.8. The two contradict each other. Run ID: abc123-def456"*

Compare that with *"It does not work"* — the first one lets us find and fix the problem, the second does not.

**Tips:**
- Take a screenshot of the entire tool screen (not just a detail) so we can see the full context.
- If the tool displayed a **Run ID** (an alphanumeric code in the results area), copy it into the report — it helps us find the logs.
- If the problem is in the output, include both the tool's output and the correct data if you have it.

**Do not** use the bug report form for feature ideas or general opinions — use the bi-monthly survey for those.

### 2. Feedback Survey — at the end of the testing period

At the end of the testing period, we will send you a survey (15–20 minutes) to collect your overall impressions and feedback: usefulness, accuracy, trust, and what you would improve.

---

## Timeline

The testing period runs from **February 16th to March 16th, 2026**.

**Before you start**: Read the [Instructions page]({INSTRUCTIONS_PAGE_URL}), verify you can access the tool, and complete the "time baseline" questionnaire (5 minutes, one time only).

---

## Practical notes

- **This is a prototype**: errors are expected and valuable. Do not hesitate to report problems — that is exactly what we need.
- **External services and API keys** are managed centrally. You do not need to provide any credentials.
- **Data formats**: check the [Instructions page]({INSTRUCTIONS_PAGE_URL}) for accepted file formats and requirements for each tool.

---

## Contacts

For technical issues (cannot access, tool does not start): **Duccio Piovani — duccio.piovani@wfp.org**

For questions about the testing process: **Myriam Nyamira — miriam.nyamira@wfp.org**

---

## Bug Report Form

**{BUG_REPORT_MESSAGE}.**
"""
)
