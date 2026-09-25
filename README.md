# VAM LLM

LLM-powered report drafting for WFP VAM (Vulnerability Analysis and Mapping)
officers: it turns validated market and food security data into first drafts of
the analytical reports the teams publish.

## What this repository is — and what it is not

**It is** the home of three report drafters:

| Service | What it drafts | Input |
|---|---|---|
| **Price Bulletin Drafter** (Market Monitor) | Market Price Bulletin for a country and month | DataBridges price series, configurable food baskets |
| **MFI Report Drafter** | Market Functionality Index assessment | processed MFI dataset (CSV) |
| **Seasonal Outlook Drafter** | Seasonal Outlook for a region and report date | seasonal climate forecast maps, with an analyst review step |

**It is not** the home of the dataset validators. The **MFI Dataset Validator**
and the **Price Data Validator** — the tools that check raw datasets *before*
they are uploaded to DataBridges — live in a separate repository:

- <https://github.com/WFP-VAM/MarketAIssist>

They were removed from this codebase in September 2026, in the commit that
carries the message *"refactor(api)!: drop MFI/Price validator routers"* and the
ones around it. The state immediately before the removal is tagged
`pre-split-20260922`. Nothing here supersedes the MarketAIssist versions: since
July 2026 the Price Data Validator there has dropped its template-comparison
layer and moved to the live DataBridges reference lists, so the copies that used
to sit in this repository were already behind when they were deleted.

The two projects sit at opposite ends of the same pipeline — MarketAIssist makes
incoming data correct and publishable, VAM LLM turns validated data into
analytical reports — but they are independent codebases with independent release
cycles. Modules that look alike (`app/shared/`, `streamlit_shared.py`) are
deliberate duplicates, aligned once at the split and free to diverge.

Historical documents under `specs/` and `evals/` predate the split and still
mention the validators as part of this app. They are dated records, not live
documentation, and were deliberately left untouched.

## Running locally

```sh
python -m venv venv
venv\Scripts\activate          # Windows (source venv/bin/activate on Unix)
pip install -r requirements.txt
copy .env.example .env         # then fill in the values
streamlit run Home.py
```

The Streamlit UI calls the services in-process through
`app/streamlit_backend/dispatcher.py` — no separate backend process is needed.
`main.py` exposes the same three services as a FastAPI app for programmatic use:
`uvicorn main:app --reload`.

Tests:

```sh
python -m pytest tests -q          # full suite
python scripts/check_mfi_reliable.py   # MFI regression gate
```

## Layout

- `app/services/market_monitor/`, `app/services/mfi_drafter/`,
  `app/services/seasonal_outlook/` — one package per drafter (LangGraph graph
  without checkpointer, FastAPI router, schemas). The Seasonal Outlook keeps an
  analysis record for its analyst review; see `docs/app-overview.md`.
- `app/services/price_cache/` — DataBridges price cache (SQLite or Cloud SQL)
  and its DataBridges client, behind the Price Bulletin Drafter.
- `app/shared/` — Vertex LLM configuration, LLM call observability, async run
  store and live run metadata, retrievers, country/ISO3 mapping, report blocks,
  the DOCX exporter and the Price Bulletin basket UI helpers.
- `app/streamlit_backend/dispatcher.py` — in-process request dispatcher used by
  the Streamlit UI.
- `pages/` + `streamlit_app.py` + `streamlit_shared.py` — Streamlit UI (WFP
  theme, onboarding, instructions, one page per drafter).
- `deploy/seasonal-outlook/` — Terraform and console setup for the Seasonal
  Outlook's storage, indexes and IAM.
- `tests/` — pytest suite; `scripts/check_mfi_reliable.py` runs the MFI subset
  as a regression gate.

## Documentation

- `docs/app-overview.md` — architecture, integrations, pipelines.
- `specs/` — dated design and assessment documents, including
  `seasonal_outlook_implementation.md`, `mfi_light_workflow.md`,
  `repo_split_plan.md` and `coherence_refactor_plan.md` (the September 2026
  refactor that moved all three drafters to checkpoint-free LangGraph graphs).
  Specs marked *Historical* describe code that no longer exists.
- `evals/` — alpha-test evaluation framework (bug reports, surveys, time
  savings). Written when the app had four services; from the split onwards the
  two validators are evaluated in the MarketAIssist context.
