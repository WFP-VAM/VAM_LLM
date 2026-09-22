# VAM LLM -- Overview

## Mission

VAM LLM is an LLM-powered solution that assists VAM (Vulnerability Analysis and Mapping) officers in Country Offices and Global HQ with data analysis and the drafting of analytical reports on market conditions and food security.

### Scope: this app drafts reports, it does not validate datasets

Dataset cleaning and validation before a DataBridges upload belongs to a separate project and a separate repository, **MarketAIssist** (<https://github.com/WFP-VAM/MarketAIssist>), which owns the MFI Dataset Validator and the Price Data Validator. Until September 2026 both applications shared this codebase; the validators were removed from it and are now maintained only in MarketAIssist.

The two projects still address opposite ends of the same data pipeline -- MarketAIssist makes incoming data correct and publication-ready, VAM LLM turns validated data into actionable intelligence reports -- but they are independent codebases with independent release cycles.

---

## What the App Does

The application exposes **three services** through a Streamlit frontend (with a parallel FastAPI backend for programmatic access):

| Service | Purpose |
|---|---|
| **Market Monitor Drafter** (Price Bulletin) | Produces a Market Monitor (Price Bulletin) report. Country Offices configure a required primary basket and an optional independently scoped secondary basket; immutable selections drive joint reportability, complete-component calculations, separate charts, basket-aware narratives, Red-Team QA, and DOCX export. Optional exchange-rate, fuel, livestock, and labour modules remain evidence-gated. |
| **MFI Report Generator** | Produces a full Market Functionality Index report for a given country. Loads MFI survey data (from CSV upload or the DataBridges API), retrieves contextual news from Seerist and ReliefWeb, generates radar-chart visualisations, drafts a per-dimension analysis across the nine MFI dimensions, synthesises an executive summary, runs Red-Team QA, and exports the result as a branded DOCX document. |
| **Seasonal Outlook Drafter** | Produces a Seasonal Outlook report for a region and report date from the climate forecast maps an analyst uploads. Extracts evidence from the maps with Gemini through Vertex, **pauses for analyst review and explicit confirmation**, then drafts, reviews and redrafts the report. Exports Word (with or without map appendix) and an artifact ZIP containing rules, inputs, original images, every evidence version and the model requests and responses. |

---

## Architecture

```
                       +-----------------+
                       |   Streamlit UI  |  (Home.py / pages/)
                       +--------+--------+
                                |
                   local call or HTTP
                                |
                       +--------v--------+
                       |   FastAPI API   |  (main.py)
                       +--------+--------+
                                |
          +----------------+----------------+
          |                |                |
   Market Monitor     MFI Drafter    Seasonal Outlook
     (router +         (router +       (router +
      graph)            graph)          service)
          |                |                |
          +----------------+----------------+
                                |
                       +--------v--------+
                       |  Shared Layer   |
                       |  - LLM (Vertex) |
                       |  - Retrievers   |
                       |  - DataBridges  |
                       |  - Async Runs   |
                       |  - DOCX Export  |
                       +-----------------+
```

Each service is a self-contained FastAPI router whose core logic lives in a **LangGraph state-machine** (`graph.py`). The shared layer provides:

- **LLM singleton** -- Google Vertex AI (Gemini 2.5 Pro by default), zero temperature, with retry and timeout policies.
- **Retrievers** -- Seerist and ReliefWeb clients that fetch contextual news for 60+ WFP-relevant countries.
- **DataBridges client** -- OAuth2-authenticated access to WFP's DataBridges API for MFI survey data and price time-series.
- **Async run manager** -- tracks long-running jobs with progress, warnings, and artifacts; pluggable backend (in-memory for dev, Firestore + GCS for production).
- **DOCX exporter** -- converts an abstract `ReportBlock` model (headings, paragraphs, tables, figures, definition boxes, references) into a branded Word document with embedded visualisations.

---

## MFI Dimensions

The Market Functionality Index is scored across nine dimensions, each on a 0-10 scale:

| Dimension | What it measures |
|---|---|
| Assortment | Variety of goods available |
| Availability | Stock presence and sufficiency |
| Price | Price levels and affordability |
| Resilience | Market capacity to absorb shocks |
| Competition | Number and diversity of traders |
| Infrastructure | Physical market infrastructure |
| Service | Quality of market services |
| Food Quality | Safety and quality of food products |
| Access & Protection | Physical access and safety for consumers |

Risk classification: **Very High** (< 4.0), **High** (4.0 -- 5.5), **Medium** (5.5 -- 7.0), **Low** (>= 7.0).

---

## External Integrations

| System | Role |
|---|---|
| **WFP DataBridges** | Primary source for MFI survey data and commodity price time-series. OAuth2 client-credentials flow. |
| **Seerist** | Intelligence/news aggregation. Provides contextual documents on markets, prices, inflation, currency, and trade for a given country and time window. |
| **ReliefWeb** | UN humanitarian reporting. Supplements Seerist with reports on food security and market conditions. |
| **Trading Economics** | Exchange-rate data for 15+ currencies used in the Market Monitor. |
| **Google Vertex AI** | LLM backend. Gemini 2.5 Pro for the Market Monitor and MFI drafters; Gemini 3.1 Pro through the Google Gen AI SDK for the Seasonal Outlook. Powers narrative generation, event extraction, trend analysis, map evidence extraction, and Red-Team QA. |
| **Google Cloud Storage** | Stores run artifacts and cached reference data in production. |
| **Google Firestore** | Persistent run-state tracking in production. |

---

## Tech Stack

| Layer | Technologies |
|---|---|
| Frontend | Streamlit (WFP-branded theme) |
| Backend API | FastAPI, Uvicorn |
| Workflow orchestration | LangGraph (state machines with conditional routing and correction loops) |
| LLM integration | LangChain (langchain-core, langchain-google-vertexai) |
| Data processing | Pandas, NumPy |
| Visualisation | Matplotlib (charts exported as Base64 PNG) |
| Report export | python-docx |
| Cloud infrastructure | Google Cloud (Vertex AI, Firestore, GCS) |
| Containerisation | Docker (Python 3.11-slim) |

---

## Repository Layout

```
UNIFIED APP/
  Home.py                      # Streamlit entry point
  streamlit_app.py             # Landing page with service navigation
  streamlit_shared.py          # Shared UI components and WFP theme
  main.py                      # FastAPI application
  start.sh                     # Docker CMD (launches Streamlit)
  Dockerfile                   # Container image
  requirements.txt             # Python dependencies
  .env.example                 # Environment variable template

  app/
    shared/
      llm.py                   # LLM singleton (Vertex AI)
      async_runs.py            # Run lifecycle & artifact management
      retrievers.py            # Seerist and ReliefWeb clients
      databridges.py           # WFP DataBridges API client
      countries.py             # Country name/ISO3 resolution
      report_blocks.py         # Abstract report block model
      docx_export.py           # DOCX rendering engine
      gcs.py                   # Google Cloud Storage helpers
      live_outputs.py          # Real-time run metadata formatting

    services/
      mfi_drafter/             # MFI report generation
        router.py, graph.py, schemas.py, data_loader.py, light_service.py
      market_monitor/          # Market Monitor generation
        router.py, graph.py, schemas.py, data_loader.py
      price_cache/             # DataBridges price cache used by Market Monitor
        config.py, sql_repository.py, refresh_worker.py, migrations/
      seasonal_outlook/        # Seasonal Outlook drafting
        router.py, api.py, service.py, provider.py, storage.py, science/

    streamlit_backend/
      dispatcher.py            # Local request dispatcher (bypasses HTTP)

  pages/
    0_Tester_Onboarding.py     # Onboarding guide for testers
    1_How_To_Use_The_Tools.py  # Usage instructions
    3_Price_Bulletin_Drafter.py # Market Monitor UI
    4_MFI_Drafter.py           # MFI Report Generator UI
    5_Seasonal_Outlook_Drafter.py # Seasonal Outlook UI

  tests/                       # Integration and unit tests
```

---

## Processing Pipelines

### Report Generation

```
User input  -->  Data loading (CSV / DataBridges API / mock)
            -->  External retrieval (Seerist + ReliefWeb, in parallel)
            -->  Context extraction and summarisation (LLM)
            -->  Visualisation generation (Matplotlib --> Base64 PNG)
            -->  Section drafting (LLM, per dimension or per module)
            -->  Executive summary / highlights (LLM)
            -->  Red-Team QA with correction loop (LLM)
            -->  DOCX export with embedded charts and WFP branding
```

### Seasonal Outlook

```
Map upload  -->  Input freeze (region, report date, 1-12 images)
            -->  Evidence extraction from images (Gemini, structured output)
            -->  Visual review and refinement
            -->  ANALYST PAUSE: review, revise (new version) or confirm
            -->  Report drafting, textual review, complete redraft
            -->  Word export (with/without map appendix) + audit ZIP
```

---

## Deployment

The application is containerised with Docker. The `start.sh` script launches **Streamlit only** on port 8080 (the current `docker-streamlit-only` branch configuration). In this mode the Streamlit frontend calls service logic directly through the in-process dispatcher rather than over HTTP to a separate FastAPI process.

For production, the app supports:

- **Firestore + GCS** backend for persistent run tracking and artifact storage (toggled via `RUNS_BACKEND=firestore_gcs`).
- **Google Vertex AI** authentication via service account or application-default credentials.
- **CORS** configuration for cross-origin API access when the FastAPI backend is exposed separately.
- **Reversible second-basket rollout** via `MARKET_MONITOR_SECOND_BASKET_ENABLED`. It defaults to enabled; setting it to `false` blocks new secondary configuration and selection while preserving history and completed report exports.
