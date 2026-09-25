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
| **MFI Report Generator** | Produces a full Market Functionality Index report for a given country from the processed MFI dataset (CSV upload). Builds a deterministic assessment profile, retrieves contextual news from Seerist and ReliefWeb, renders charts and maps, drafts the nine dimension sections and the selected market sections, has each family reviewed and corrected when the review asks for it, then writes the executive summary and exports a branded DOCX document. |
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
      graph)            graph)          graph)
          |                |                |
          +----------------+----------------+
                                |
                       +--------v--------+
                       |  Shared Layer   |
                       |  - LLM (Vertex) |
                       |  - Retrievers   |
                       |  - Async Runs   |
                       |  - DOCX Export  |
                       +-----------------+
```

Each service is a self-contained FastAPI router whose workflow is a **LangGraph graph**: `market_monitor/graph.py`, `mfi_drafter/light_graph.py` and `seasonal_outlook/graph.py`. None of the graphs uses a checkpointer, and no service has a checkpoint or resume layer:

- **Market Monitor and MFI** run a report in a background thread and keep only a run record (status, progress, result, artifacts). A failed report is run again.
- **Seasonal Outlook** has one graph with three entry points, one per phase: extraction (extraction, visual review, refinement), analyst feedback, and report (draft, review, redraft, export). Each phase is one run of the graph. The analyst's review happens *between* two runs, so the service keeps an **analysis record** in Firestore and GCS: inputs, immutable evidence versions, comments, the confirmation of one exact evidence version, and each operation's calls and final output. A failed phase is retried from the same inputs.

The shared layer provides:

- **Vertex model factory** (`llm.py`) -- the Market Monitor's Gemini models (Gemini 2.5 Pro by default), zero temperature, cached per timeout and retry setting. The MFI drafter and the Seasonal Outlook configure their own Vertex clients.
- **Retrievers** -- Seerist and ReliefWeb clients that fetch contextual news for 60+ WFP-relevant countries.
- **Async run manager** -- tracks long-running jobs with progress, warnings, and artifacts; pluggable backend (in-memory for dev, Firestore + GCS for production).
- **DOCX exporter** -- converts an abstract `ReportBlock` model (headings, paragraphs, tables, figures, notices, references) into a branded Word document with embedded visualisations.

The DataBridges client for price data lives in the price cache (`app/services/price_cache/`), which the Market Monitor reads.

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
| **WFP DataBridges** | Source of the commodity price time-series and exchange rates behind the Market Monitor, cached by the price cache. OAuth2 client-credentials flow. |
| **Seerist** | Intelligence/news aggregation. Provides contextual documents on markets, prices, inflation, currency, and trade for a given country and time window. |
| **ReliefWeb** | UN humanitarian reporting. Supplements Seerist with reports on food security and market conditions. |
| **Trading Economics** | Exchange-rate data for 15+ currencies used in the Market Monitor. |
| **Google Vertex AI** | LLM backend. Gemini 2.5 Pro for the Market Monitor; Gemini 3.1 Pro for the MFI drafter (LangChain Vertex client) and for the Seasonal Outlook (Google Gen AI SDK). Powers narrative generation, event extraction, trend analysis, map evidence extraction, review and QA. |
| **Google Cloud Storage** | Stores run artifacts, Seasonal Outlook inputs, evidence versions and exports, and cached reference data in production. |
| **Google Firestore** | Persistent run records and Seasonal Outlook analysis records in production. |

---

## Tech Stack

| Layer | Technologies |
|---|---|
| Frontend | Streamlit (WFP-branded theme) |
| Backend API | FastAPI, Uvicorn |
| Workflow orchestration | LangGraph (one graph per service, no checkpointer) |
| LLM integration | LangChain (langchain-core, langchain-google-vertexai); Google Gen AI SDK for the Seasonal Outlook |
| Data processing | Pandas, NumPy |
| Visualisation | Matplotlib (charts exported as Base64 PNG) |
| Report export | python-docx |
| Cloud infrastructure | Google Cloud (Vertex AI, Firestore, GCS, Cloud SQL for the price cache) |
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
      llm.py                   # Vertex model factory (Market Monitor)
      llm_observability.py     # LLM call tracing
      async_runs.py            # Run lifecycle & artifact management
      retrievers.py            # Seerist and ReliefWeb clients
      countries.py             # Country name/ISO3 resolution
      report_blocks.py         # Abstract report block model
      docx_export.py           # DOCX rendering engine
      live_outputs.py          # Real-time run metadata formatting
      market_monitor_basket_ui.py # Price Bulletin basket configuration helpers

    services/
      mfi_drafter/             # MFI report generation
        router.py, light_graph.py, light_service.py, light_runtime.py, schemas.py, data_loader.py
      market_monitor/          # Market Monitor generation
        router.py, graph.py, schemas.py, data_loader.py
      price_cache/             # DataBridges price cache used by Market Monitor
        config.py, sql_repository.py, databridges_adapter.py, refresh_worker.py, migrations/
      seasonal_outlook/        # Seasonal Outlook drafting
        router.py, api.py, service.py, graph.py, runner.py, engine.py, provider.py, storage.py, science/

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

### Market Monitor (Price Bulletin)

```
User input  -->  Price data from the price cache (DataBridges), or mock data on request
            -->  Visualisation generation (Matplotlib --> Base64 PNG)
            -->  News retrieval (Seerist + ReliefWeb)
            -->  Event mapping and trend analysis (LLM)
            -->  Optional modules (exchange rate, fuel, livestock, labour)
            -->  Highlights and narrative drafting (LLM)
            -->  Red-Team QA with correction loop (LLM)
            -->  DOCX export with embedded charts and WFP branding
```

### MFI Report

```
Processed MFI CSV  -->  Deterministic assessment profile (no LLM)
                   -->  Context retrieval (Seerist + ReliefWeb)
                   -->  Charts and maps, in parallel with drafting
                   -->  Dimension sections | market sections (LLM, in parallel)
                   -->  Review of each family (LLM); correction where the review asks for it
                   -->  Executive summary and country context (LLM)
                   -->  Report blocks  -->  DOCX export
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

Each arrow group before and after the pause is one phase of the Seasonal graph; the pause itself is kept in the analysis record, not in the graph.

---

## Deployment

The application is containerised with Docker. The `start.sh` script launches **Streamlit only** on port 8080 (the current `docker-streamlit-only` branch configuration). In this mode the Streamlit frontend calls service logic directly through the in-process dispatcher rather than over HTTP to a separate FastAPI process.

Long-running work -- Market Monitor and MFI reports, Seasonal Outlook phases -- runs in background threads of that same process, so the Cloud Run service needs CPU always allocated and enough memory for the Word and ZIP exports. If an instance stops mid-run, the report or phase fails (a Seasonal phase shows as interrupted once its deadline passes) and is run again.

For production, the app supports:

- **Firestore + GCS** backend for persistent run tracking and artifact storage (toggled via `RUNS_BACKEND=firestore_gcs`).
- **Seasonal Outlook storage and IAM** (`deploy/seasonal-outlook/`): the analysis collection, bucket, history indexes and the download-link signer, configured through the `SEASONAL_*` variables. The app identity needs `roles/aiplatform.user` in `SEASONAL_PROJECT`.
- **Google Vertex AI** authentication via service account or application-default credentials.
- **CORS** configuration for cross-origin API access when the FastAPI backend is exposed separately.
- **Reversible second-basket rollout** via `MARKET_MONITOR_SECOND_BASKET_ENABLED`. It defaults to enabled; setting it to `false` blocks new secondary configuration and selection while preserving history and completed report exports.
