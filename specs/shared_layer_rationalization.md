# Shared layer rationalization: one LLM client, one observability layer, one run infrastructure

**Status: approved 25 September 2026; all decisions taken (§6).** Based on a read-only analysis of `VAM-LLM-Sep2026` @ `41d76af`, after the coherence refactor (`coherence_refactor_plan.md`). Branches:
- The existing defects of §3.6 that D8 covers are fixed on `fix/post-phase7`. They ship once the coherence refactor is accepted on GCP (its Phase 7).
- The refactor itself happens on `refactor/shared-layer`, which is based on those fixes. Nothing from it is merged or deployed before that acceptance.

---

## 1. Summary

Today each drafter calls the model, records its calls and manages its runs in its own way:

- **Three LLM stacks.**
  - Market Monitor (MM) goes through `app/shared/llm.py`, using LangChain `ChatVertexAI`.
  - MFI builds its own `ChatVertexAI` and relies on private helpers of the shared module and of LangChain.
  - Seasonal Outlook uses a different SDK (`google-genai`) and imports nothing from `app/shared`.
- **Three call recorders.**
  - Only MM uses `llm_observability.py`.
  - MFI copies its field names but gives some of them different meanings.
  - Seasonal has an audit recorder without call ids, failure codes or logs.
- **Two run infrastructures.** MM and MFI use `async_runs.py`, a simple status store. Seasonal built a more robust store of its own.
- **Duplicated endpoints.** MM and MFI implement every endpoint twice, once in the FastAPI router and once in the Streamlit dispatcher. Production uses the dispatcher copy, and it has drifted from the router.

The proposal, in one line per area:

1. **One LLM client** (`app/shared/llm/`). Every model call goes through `LLMClient`, using a per-drafter model profile and one SDK. Drafters keep their prompts, schemas, validators and domain repair logic.
2. **Observability built into that client.** Every call produces the same record and reaches the same outputs: the live run view, structured logs and optional payload capture. Seasonal's mandatory audit becomes one more output. No call can bypass it.
3. **One run infrastructure** (`app/shared/runs/`).
   - Seasonal's store, launcher, deadlines and late-write guards become shared.
   - MM and MFI runs are rebuilt on them, replacing `async_runs.py`.
   - Seasonal keeps its own record format on the same primitives.
4. **One implementation per endpoint** (recommended, outside `app/shared`). The dispatcher stops re-implementing MM and MFI.
5. **`app/shared` holds only shared code.** Drafter-specific code moves into its drafter, and shared code stops importing drafters.

§3.3 explains why Seasonal does not use `async_runs`.

## 2. Principles

1. **One way to call a model.** A model call outside `app/shared/llm/` is a test failure.
2. **Every call is observed the same way.** There is one record schema and one tracer. Where the records go (UI, logs, payload capture, audit) is configured per drafter.
3. **One run infrastructure.** MM and MFI get the guarantees Seasonal already has.
4. **Behaviour-preserving migration.** Each drafter moves separately. Each move is checked against snapshots of the exact model requests, using the tooling from the coherence refactor.
5. **Portability seams for the later AWS move** (`aws_step_functions_assessment.md`). The model provider, the store and the background executor are each an interface with one GCP implementation today. A later Bedrock provider, DynamoDB/S3 store or Step Functions runner would then replace one implementation, not three drafters.
6. **Nothing visible changes unless it is listed.** Prompts, models, analytics, reports, environment variable names and stored data stay as they are, except for the changes in §6.

---

## 3. Current state

### 3.1 LLM access

| | Market Monitor | MFI Drafter | Seasonal Outlook |
|---|---|---|---|
| Entry point | `app/shared/llm.py` `get_model()` | its own client in `light_runtime.py` | `VertexProvider` in `provider.py` |
| SDK | LangChain `ChatVertexAI` | LangChain `ChatVertexAI`, plus private LangChain/GAPIC calls for token counting and schema checks | `google-genai` |
| Client reuse | cache keyed by settings | `lru_cache` | new client for every call |
| Model | `LLM_MODEL` (default `gemini-2.5-pro`) | `gemini-3.1-pro-preview`, hard-coded | `SEASONAL_MODEL` (default `gemini-3.1-pro-preview`) |
| Location | `VERTEX_LOCATION` (default `us-central1`) | `global`, hard-coded | `global` |
| Project | env chain, then ADC | the shared *private* helper | `SEASONAL_PROJECT` only |
| Generation settings | temperature 0; JSON requested in the prompt text | temperature 1.0; 65,536 output tokens; response schema | temperature 1.0; thinking HIGH; media resolution HIGH; response schema |
| Timeout | 90 s (`LLM_TIMEOUT_SECONDS`) | 600 s (180 s for the summary) | 600 s (1200 or 1800 s on retry) |
| Retries | SDK: `LLM_MAX_RETRIES=2` total attempts, on *any* Google API error, including permission denied | 2 attempts on transient errors, repairing missing sections | none; the analyst retries the whole phase |
| Token budget | none | character budget and CountTokens; oversized requests are split | none |
| Truncated response | recorded, not checked | detected and repaired | fails the phase |
| Pydantic-to-Gemini schema | not used | its own converter | a second, different converter |
| Calls | up to ~30 per report, sequential | 5–7 per report, 2 in parallel | 3 (extract), 1 (feedback), 3 (report) |

Each of the following is implemented two or three times:
- project discovery;
- configuration reading;
- client caching;
- response-text extraction;
- JSON parsing (three levels of leniency);
- finish-reason checks;
- retry loops;
- error classification;
- prompt hashing;
- token-usage normalisation;
- timestamps.

### 3.2 Observability

| | Market Monitor | MFI | Seasonal |
|---|---|---|---|
| Recorder | shared `LLMTraceSession` | own `RunLedger` (same field names, some with other meanings) | own `Recorder` |
| Call ids | yes | yes, in a different format | none |
| Failure codes | 4 codes × 4 stages | 2 generic codes, no stage | free text |
| Tokens | normalised, but thought tokens are always empty (bug) | prompt, candidate, total | raw provider dictionary |
| Structured logs | `app.llm_trace` JSON lines | none | none |
| Payloads | opt-in capture to a private GCS prefix (30 days) | none, yet `/info` reports the shared capture settings | always stored, kept permanently, included in the audit ZIP |
| Live view | current call, counters, last failure | counters only, no current call; a failed run's last update still says "running" | stage progress; calls shown as raw JSON |
| `/info` and `/health` | both | both | `/info` only |
| Error text | sanitised | exception class name only | not sanitised; may contain model output |

Only `main.py` configures logging. Production runs the Streamlit process, which configures none, so application INFO logs are probably not emitted there. The exception is `app.llm_trace`, which installs its own handler.

### 3.3 Runs, and why Seasonal does not use `async_runs`

| | `async_runs.py` (MM, MFI) | Seasonal store (`storage.py`, `service.py`, `runner.py`) |
|---|---|---|
| Model | one job: pending → running → completed or failed | an analysis with several operations, human pauses and evidence versions |
| Writes | read, then merge; no transaction | Firestore transactions, revision numbers, 409 on conflict |
| Background launch | 4 places: 2 `BackgroundTasks`, 2 dispatcher threads | one launcher |
| Worker dies | the run stays "running" forever | the deadline passes, the run shows "interrupted", and late threads are blocked from writing |
| Repeated requests | none | idempotent request ids |
| History | none; the UI remembers only the last run | filtered list backed by composite indexes |
| Large downloads | streamed through the app | signed URLs |
| Storage error | one failed write switches the whole process to memory, silently, and earlier runs become invisible | fails closed |
| Completion | two separate writes | one transaction |

Seasonal does not use `async_runs` because `async_runs` cannot hold what Seasonal needs:
- an analysis spans several operations separated by human review;
- evidence versions must be immutable;
- two analysts may act on the same analysis at once;
- actions must be idempotent;
- analyses must be searchable;
- large files are downloaded through signed URLs;
- every model request and response is kept as an audit trail.

Its own store ended up more robust than `async_runs` on every row above. **So Seasonal does not need `async_runs`, but the app should not keep two run infrastructures.** The proposal reverses the direction: Seasonal's primitives become the shared infrastructure (§4.4).

### 3.4 Duplicated endpoints

The dispatcher (`app/streamlit_backend/dispatcher.py`, 1,793 lines) re-implements every MM and MFI endpoint next to the FastAPI routers. Only Seasonal delegates both transports to one implementation (`seasonal_outlook/api.handle`). Since the container runs Streamlit only, production uses the dispatcher copies, which have drifted from the routers:
- MM Word export ignores the report language, so the "References" heading is English in French and Spanish bulletins downloaded from the app.
- MM runs omit the language from their metadata, and their live-output titles are not translated.
- MM `/info` lacks the `language` input.
- MM `/cache/refreshes` is missing.
- Responses are never validated.

The background-run code, the status, artifact and export routes, and the live-output blocks exist in 2–4 copies each.

### 3.5 The rest of `app/shared`

| Module | Lines | Used by | Issue |
|---|---:|---|---|
| `__init__.py` | 22 | nobody | lazy exports that nothing imports |
| `llm.py` | 208 | MM (MFI borrows a private helper) | MM-only in practice; loads `.env` as an import side effect |
| `llm_observability.py` | 1,103 | MM (MFI uses only its schema and settings) | MM-only in practice; `invoke_json` and `invoke_text` are near copies that have drifted |
| `async_runs.py` | 843 | MM, MFI | see §3.3 |
| `live_outputs.py` | 263 | MM, MFI | the DataBridges part is MM-only |
| `report_blocks.py` | 592 | MM, MFI | about 70% is the MM report builder; the MFI layout contract is also here |
| `docx_export.py` | 557 | MM, MFI | contains MFI styling and the MM basket table; imports MM's i18n, so shared depends on a drafter |
| `retrievers.py` | 699 | MM, MFI | loads `.env` at import; MM and MFI each re-implement fetch, merge and dedupe around it |
| `countries.py` | 164 | MM, MFI, price cache, retrievers | fine |
| `market_monitor_basket_ui.py` | 430 | the Price Bulletin page | drafter page code in shared |

Small helpers are duplicated across the whole app:

| Helper | Copies |
|---|---:|
| boolean env parsers | 5 |
| "serialise anything" JSON encoders | 4 |
| fingerprint functions | 4 |
| "UTC now" helpers | 6 |
| `gs://` parsers | 2 |
| GCS writers | 3 |
| slug helpers | 6 |
| secret redactors | 3 |

Seasonal writes its Word documents with raw python-docx. `streamlit_shared.py` (1,129 lines) mixes generic UI with MM and MFI specifics. The three pages poll for progress in three different ways.

### 3.6 Existing defects found during the analysis

Independent of the refactor; most are small fixes. Items 1–4 and 9 are fixed on `fix/post-phase7` (D8). The others are handled by the phases noted in brackets.

1. MM Word export from the app ignores the language (§3.4).
2. MM runs started from the app omit the language from their metadata, leave live-output titles untranslated, and `/info` lacks the `language` input.
3. `async_runs` switches the whole process to memory after one failed Firestore write, and runs stored before that become invisible.
4. No logging configuration in the Streamlit process.
5. `LLM_MAX_RETRIES` [Phase 2]:
   - In the installed LangChain it is a *total* number of attempts, so 2 means one retry.
   - It retries every Google API error, including permission denied and invalid argument, with 4–10 s backoff.
6. MM diagnostics: `thought_tokens` is always empty, and `provider_response_id` holds a LangChain run id [Phase 2].
7. MFI live diagnostics [Phase 2]:
   - A failed run's last update still says "running".
   - When one parallel branch fails, the other branch still finishes its paid call.
8. MFI (FastAPI path only): model output errors come back as a 400 "CSV validation error" [Phase 4].
9. Seasonal:
   - `GET /runs/{id}` exposes `gs://` URIs, including the bucket name.
   - Call errors are stored unsanitised.
10. From Phase 7: `requirements.txt` is unpinned. MFI's private LangChain calls are the most exposed to an upgrade. You chose not to pin; the Cloud Build log is compared with the tested versions instead.
11. Found in Phase 5: artifact downloads through the API name their file with a `.docx` suffix (for example `…price-data.csv.docx`), because the `Content-Disposition` helper was written for the Word exports. The pages name their downloads themselves, so only API clients see it [not scheduled; awaiting your decision].

---

## 4. Target design

### 4.1 Layout

```
app/shared/
  config.py          .env loading at the entry points, typed env readers, project resolution, logging setup
  util.py            canonical JSON, fingerprints, UTC timestamps, slugs, secret redaction
  cloud.py           cached Firestore/Storage clients, gs:// parsing, blob writes and reads, signed URLs
  llm/
    __init__.py      public API
    profiles.py      one model profile per drafter, with its env overrides; status for /info
    client.py        LLMClient: generate, generate_json, count_tokens; retry policy; tracing hooks
    vertex.py        google-genai provider: the only SDK-specific code
    schema.py        Pydantic → Gemini schema, response text, JSON parsing, finish reasons
    errors.py        error taxonomy, transient classification, HTTP mapping
    tracing.py       call record, run trace, sinks (live, logs, payload capture, audit)
  runs/
    store.py         transactional document store + content-addressed blob store; GCP and memory backends
    executor.py      background launch, deadlines → interrupted, guards against late writers
    report_runs.py   the MM/MFI run record (replaces async_runs.py)
    live_outputs.py  live previews and their artifacts (generic part)
  documents/
    blocks.py        ReportBlock
    docx.py          generic Word renderer; drafters supply a theme and labels
  context/
    retrievers.py    Seerist, ReliefWeb
    news.py          shared fetch, merge and dedupe of context documents (MM and MFI)
  countries.py
```

"One place" here means one package per concern, each with one public entry point. It does not mean one file: together these concerns are several thousand lines.

What leaves `app/shared`:

| Today | Moves to |
|---|---|
| `market_monitor_basket_ui.py` | `market_monitor/basket_ui.py` |
| MM report builder in `report_blocks.py`; the MM basket table in `docx_export.py` | `market_monitor/report_blocks.py` |
| MFI layout contract in `report_blocks.py`; MFI styling in `docx_export.py` | `mfi_drafter/report_layout.py`, which hands a theme to the shared renderer |
| the import of MM's i18n inside shared | labels passed in as parameters |
| MM and MFI branches of `streamlit_shared.py` | drafter UI modules, as Seasonal already does with `ui.py` |

### 4.2 LLM client

```python
from app.shared.llm import LLMClient, LLMRequest, profiles

client = LLMClient(profiles.MFI, tracer=tracer)          # one client per run, passed explicitly
reply = client.generate(LLMRequest(
    operation="mfi.light.draft_dimensions.v1", node="draft_dimensions", work_item=work_id,
    contents=[prompt], response_schema=SectionsResponse))
# reply: text, finish_reason, usage (prompt/candidate/thought/total), response_id, served_model
value = client.generate_json(request, validator=inspect_sections)   # parse and validate, or raise LLMError
tokens = client.count_tokens(request)
```

- **Profiles reproduce today's settings exactly** (table in §3.1).
  - MM keeps `gemini-2.5-pro`, `us-central1`, temperature 0 and 90 s.
  - MFI keeps its fixed contract.
  - Seasonal keeps its `SEASONAL_*` settings.
  - Existing environment variable names do not change.
- **One SDK: `google-genai`** (D1).
  - Seasonal already uses it.
  - It covers everything the three drafters need: thinking, media resolution, response schemas, CountTokens, `gs://` image parts, per-request timeouts and retry options.
  - It removes MFI's calls to private LangChain and GAPIC functions, which any upgrade can break.
  - It lets us drop `langchain-google-vertexai` and `google-cloud-aiplatform` if nothing else needs them. LangGraph keeps `langchain-core`.
  - MM sends single user messages without tools, so LangChain adds nothing there.
- **Retries belong to the client** (D3).
  - SDK retries are always off. The client retries only transient errors (timeouts, 429, 500, 503, aborted, connection errors), up to the profile's attempts, with backoff, and records each attempt.
  - Permission denied, invalid argument and blocked responses fail immediately.
- **Truncated and blocked responses are reported the same way everywhere.** Each drafter decides what to do: MFI repairs, Seasonal fails, MM per D5.
- **MFI's content repair stays in MFI.** Re-requesting missing sections is domain logic. It calls the client again, and the record links the repair to the failed attempt.
- **Token counting and budgets** are available to every drafter.
- **Optional concurrency limit** per model, to protect quota when several reports run at once.
- **Explicit wiring.** The client reaches graph nodes explicitly, through the graph builder or the LangGraph config, not through a ContextVar.
- **Test guard.** A test fails if any module outside `app/shared/llm/` imports `google.genai`, `langchain_google_vertexai`, `vertexai` or `google.cloud.aiplatform`.

### 4.3 Observability

- **One call record for every call.** It extends today's `LLMCallDiagnostic` with these fields:
  - identity: call id, sequence, service, run id, operation, node, work item (MFI work id or Seasonal stage), attempt, link to the attempt being repaired;
  - model: requested and served model, location, timeout;
  - timing: start, end, duration;
  - outcome: status, failure code, stage, transient flag, sanitised error;
  - content: prompt and response sizes and hashes, finish reason, normalised token usage (including thought tokens), response id.
- **One run trace.** It holds counters, token totals and the list of active calls; MFI runs two at once, so a single "current call" is not enough. A failed attempt followed by a successful retry counts as *recovered*, not as a failure.
- **Sinks, configured per run:**

| Sink | MM | MFI | Seasonal |
|---|---|---|---|
| Live run view | run record | run record | the operation's `calls` |
| Structured logs (`app.llm_trace`) | yes | yes (new) | yes (new) |
| Payload capture: opt-in, private GCS prefix, 30-day lifecycle | yes | yes, new (D4) | not needed |
| Audit: mandatory, written before validation; if it cannot be written, the call fails | – | – | yes (today's behaviour) |

- **Same privacy rules everywhere.** Error text is sanitised the same way, and bucket names never appear in API responses.
- **Status endpoints.** One `llm_status()` feeds `/info` and `/health` for all three services; Seasonal gains `/health`.
- **UI.** One diagnostics panel for all three pages.
- **Logging.** Logging is configured once for the Streamlit process, as JSON on stdout, which Cloud Logging collects.
- **Compatibility.**
  - Stored MM and MFI results still load, because trace schema 1.0 is still accepted.
  - Seasonal's call entries only gain fields. There is no workflow-revision bump, so existing analyses stay open.

### 4.4 Runs

- **`store.py`**, extracted from `seasonal_outlook/storage.py`:
  - a document store: get, transactional mutate, filtered and ordered list, size guard;
  - a blob store: content-addressed create-only writes, checksum-verified reads, signed URLs;
  - Firestore/GCS and memory backends.
- **`executor.py`**, extracted from Seasonal's `service.py` and `runner.py`:
  - `launch()`: a daemon thread today. The Step Functions/ECS runner of the AWS assessment would implement the same interface.
  - Deadline expiry into "interrupted", checked when a run is read.
  - A transactional guard that blocks late writers.
- **`report_runs.py`** replaces `async_runs.py` for MM and MFI.
  - It keeps the same function names (`create_run`, `get_run`, `update_run`, `set_run_completed`, `set_run_failed`, `add_run_artifact`, `get_run_artifact`), so call sites barely change.
  - New: service and id fields.
  - New: a per-drafter deadline after which the run shows "interrupted".
  - New: completion in one write.
  - New: `list_runs(service)` for history.
  - New: when durable storage is configured but unreachable, it fails instead of falling back silently. Without durable storage it uses memory, as today.
  - Records written by `async_runs` stay readable.
- **Seasonal** keeps its analysis record, operations, versions and API. It swaps its private store, launcher and guards for the shared ones, with identical behaviour checked by snapshot.
- **Later option (D6).** MM and MFI runs could become single-operation records with Seasonal's shape. One status, progress, diagnostics and history UI would then serve all three drafters.

### 4.5 One implementation per endpoint (recommended; outside `app/shared`)

- **(a) Seasonal's pattern.** Each drafter exposes one `api.handle(...)`, and FastAPI and the dispatcher are thin adapters around it.
- **(b) FastAPI as the only implementation.** The typed FastAPI routers stay, and the dispatcher calls the FastAPI app in-process over ASGI, with no network. Background work moves from `BackgroundTasks` to the shared launcher. This keeps OpenAPI docs and request and response validation for all drafters.

Recommendation: (b), after a short spike confirming that the in-process call works from Streamlit's threads; otherwise (a). Either option removes about 1,500 duplicated dispatcher lines and the drift listed in §3.4.

### 4.6 Other modules

- **documents.** `ReportBlock` stays shared. The builders move into the drafters, and the renderer takes a theme and labels. Seasonal's Word export can adopt it later (optional).
- **context.** A shared `news.py` does fetch, merge and dedupe for MM and MFI. The retrievers log through the standard logger.
- **config, util, cloud.** One copy of each helper. Call sites move in the modules the phases already touch; untouched domain helpers (for example in the price cache) stay as they are.
- **Streamlit.** The generic UI stays in `streamlit_shared.py`, and drafter-specific parts move to drafter UI modules. All pages poll with the same fragment-based mechanism.

### 4.7 What does not change

- Prompts, schemas, validators, graphs, analytics and report content.
- Environment variable names, Firestore collections and buckets.
- Readability of stored records.
- UI flows, apart from the added diagnostics and history.

---

## 5. Migration plan

Work happens on `refactor/shared-layer`, which is based on `fix/post-phase7`. Merging and deploying wait until Phase 7 of the coherence refactor is accepted and the D8 fixes have shipped.

| Phase | Content | Verification | Size |
|---|---|---|---|
| 0 | **Baselines.** Exact provider requests for all three drafters through fake transports (new for MM), plus the current diagnostics and run records | – | S |
| 1 | **Foundations.** `config` (env loading at the entry points, logging, which moves out of `streamlit_shared.py`), `util` (started by D8) and `cloud`; move `market_monitor_basket_ui.py`; drop the `__init__` exports | full suite; snapshots unchanged | S |
| 2 | **LLM client and tracing on `google-genai`.** Migrate MM first, because `llm_observability.py` has no other user and can become the shared tracer instead of being duplicated; then MFI, then Seasonal. Retire `llm.py`, `llm_observability.py`, MFI's client and Seasonal's provider and `Recorder` | requests identical to the baselines (MM: same prompt text and parameters); outputs identical; complete diagnostics for all three | L |
| 3 | **Runs.** Shared store, launcher and guards extracted from Seasonal; Seasonal on them; `report_runs.py` replaces `async_runs.py` | Seasonal snapshots identical; MM/MFI lifecycle tests including "interrupted"; old records readable | M |
| 4 | **One implementation per endpoint** (D7) | API smoke tests over both transports; AppTest on every page | M–L |
| 5 | **Documents, context and UI consolidation** | Word output identical (text and styles); pages render | M |
| 6 | **Close-out.** Docs, `.env.example`, requirements; container build and suite; a no-traffic GCP revision with one run of each drafter | as in Phase 7 of the coherence refactor | S |

Each phase is a set of commits that can be released on its own. Phases 4 and 5 can be deferred without blocking 1–3.

### Progress

**D8 fixes, done 2026-09-25** on `fix/post-phase7` (4 commits, not pushed; they ship after Phase 7 acceptance):
- MM from the app: Word export, run metadata, live-output titles and `/info` now follow the report language.
- Run store: fails closed with a 503 instead of silently moving runs to memory.
- Logging: configured in the Streamlit process.
- Seasonal: API replies carry no storage URIs, and phase errors are redacted.

Full suite: 855 tests, 852 passed, 3 skipped. Each new test fails on the code before its fix.

**Phase 0, done 2026-09-25.** The tooling lives in git-ignored `.tmp/shared-layer/`:
- **What runs:** `wire_mm.py`, `wire_mfi.py` and `wire_seasonal.py` run each drafter end to end offline with its real SDK client. They record every request as it would leave for Vertex.
- **How it intercepts:**
  - LangChain: at its gRPC client, with each request converted to JSON; fields left at their default value are omitted, as they are on the wire.
  - google-genai: at the HTTP transport, with the JSON body read as sent.
- **Key spelling:** API field names are compared in camelCase, because Vertex accepts both spellings and google-genai sends dict-typed schemas and configs with the keys it was given.
- **Baselines** (`before/`):
  - MM: 12 requests, a mock-data bulletin in English and French, through LangChain.
  - MFI: 14 generation requests and 28 token counts, Benin and Haiti, through LangChain.
  - Seasonal: 21 requests, AFY, AMX and ASE, through google-genai.
- **Comparison:** after each migration, rerun the drafter's script and compare with `compare_wire.py`.
- **Deprecation:** the installed `langchain-google-vertexai` warns that `ChatVertexAI`, used by MM and MFI, is deprecated since 3.2.0 and will be removed in 4.0.

**Phase 1, done 2026-09-25** on `refactor/shared-layer`:
- `app/shared/config.py` provides `load_environment`, `configure_logging` and `resolve_project`.
- `main.py` and `streamlit_shared.py` call the first two before importing any service. The Price Bulletin page now imports `streamlit_shared` first, like the other pages.
- MFI uses `resolve_project` instead of a private helper of `llm.py`.
- `market_monitor_basket_ui.py` is now `market_monitor/basket_ui.py`.
- `app/shared/__init__.py` exports nothing.
- Deferred on purpose:
  - The import-time `load_dotenv()` in `llm.py` and `retrievers.py` stays until Phases 2 and 5 retire or move those modules, so no process loses its environment in between.
  - `cloud.py` arrives with its first users in Phases 2–3 rather than empty.
- Verification: full suite 855 tests, 852 passed, 3 skipped, with no outcome changed from the fix branch. MFI and Seasonal snapshots identical. Every page renders, and all first-party imports resolve.

**Phase 2, step 1 (the client; Market Monitor), done 2026-09-25:**
- **New package `app/shared/llm/`:**
  - `protocol`: request and response types.
  - `profiles`: each drafter's model settings.
  - `settings`: the `LLM_*` settings, moved from `llm.py` without its LangChain factory or its import-time `.env` loading.
  - `errors`: stable failure codes and the transient-error classification.
  - `schema`: JSON parsing.
  - `vertex`: the only google-genai code. One SDK client per project, location and header set, SDK retries off, and a timeout on each request.
  - `tracing`: moved from `llm_observability.py`. Records, logs, payload capture and the live sink keep their behaviour. It adds one record per attempt, retry and repair links, the list of active calls and a mandatory audit hook.
  - `client`: `LLMClient`. It retries transient transport errors only, and parsing and validation run inside the call's record.
- **Market Monitor** calls go through `LLMClient` with its profile. `llm.py` and `llm_observability.py` no longer exist; they moved into the package. Its tests replace the model through `graph.llm_provider`.
- **Wire comparison:** the 12 requests of the mock-data bulletin, in English and French, are identical to LangChain's: prompts, generation settings, timeouts, model and location. The reports built from them are identical too. For this, the harness now seeds the mock prices, pins the `LLM_*` settings, and runs the old code from a temporary worktree of the pre-migration commit.
- **Behaviour changes, as decided:**
  - only transient errors are retried (D3);
  - a truncated reply fails the call (D5);
  - each attempt is its own record, and a failed attempt that a retry fixed shows as recovered;
  - `configured_max_retries` now shows the real retry count: 1 for the default two attempts.
- **SDK guard:** a test fails if any module outside `app/shared/llm/` imports a model SDK. MFI's `light_runtime.py` and `light_contracts.py` and Seasonal's `provider.py` are exempt until their steps.
- **Verification:**
  - full suite 860 tests, 857 passed, 3 skipped, with no outcome changed;
  - 15 tests replaced (LangChain-specific, or checking `batch_id`, which no app code set) and 20 added (retries, truncation, audit hook, the provider's wire format and client reuse);
  - MFI and Seasonal snapshots identical; every page renders; imports resolve.

**Phase 2, step 2 (MFI), done 2026-09-25:**
- **MFI calls go through `LLMClient`** with the MFI profile: Gemini 3.1 Pro, `global`, temperature 1, 65,536 output tokens, 600 s (180 s for the summary).
  - `ModelRuntime` keeps its two attempts per work item, shared between retries and repairs (D3: unchanged). The profile therefore allows one attempt per call.
  - Token counts go through the client too (30 s; not traced, as before).
- **One trace per run.** MFI's own call records are gone:
  - the run's `Tracer` keeps one record per call, and the live view and the result read it;
  - the JSON logs and opt-in payload capture (D4) now cover MFI;
  - a retry or repair names the call it retries or repairs, counts as that call's next attempt, and marks it recovered when it succeeds;
  - batches count their attempts from the trace, by work item.
- **Shared client additions:** `LLMRequest.retry_of`, for drafters that run their own attempts; the attempt number follows the retry or repair link; `check_response_schema`.
- **Fixes (§3.6, item 7):**
  - a finished run pushes its final trace, so a failed run's live view says "failed", not "running";
  - once any step has failed, the other branch starts no new model call. A call already in flight still completes, because the SDK cannot cancel it.
- **Schema:**
  - Without an explicit order, Vertex generates properties alphabetically, and MFI's drafts have always followed that order (notes before sections). google-genai would send the declaration order instead, so the contract now pins the alphabetical order in `propertyOrdering`.
  - The start-up schema check uses the SDK's public `Schema` model instead of a private LangChain function, and rejects unknown types instead of warning.
- **Errors:** failed calls raise `LLMCallError`. A failed run's error names the failure code, node, operation and call; the provider's message stays in the call record.
- **Wire comparison:**
  - the 14 generation requests are identical to LangChain's, apart from the explicit alphabetical `propertyOrdering` (48 objects);
  - token counts drop from 28 to 14. LangChain rewrote the schema dict while counting, which defeated the per-run cache, so every count went out twice. Count requests also no longer carry an empty `systemInstruction`;
  - the reports built from them are identical.
- **Snapshots:**
  - Benin, Haiti and Gaza are identical to step 1, apart from `propertyOrdering` in the recorded requests and what follows from it in the effective contract: new schema hashes, and `google-genai` instead of `langchain-core` and `langchain-google-vertexai` among its dependencies.
  - The Seasonal snapshots and the MM wire comparison are unchanged.
- **SDK guard:** only Seasonal's `provider.py` is still exempt. No app module imports `langchain-google-vertexai`, `google-cloud-aiplatform` or `langchain-core` any more; the Phase 2 cleanup drops them from the requirements.
- **Verification:**
  - full suite 869 tests, 866 passed, 3 skipped, with no outcome changed;
  - MFI's fakes now implement the provider interface. The LangChain request test became a google-genai wire test (the `vertex_wire` fixture moved to `tests/conftest.py`), and 9 cases were added: retry and repair links, stopped branches, the pinned order, the stricter schema check and an empty reply;
  - every page renders and all modules import.

**Phase 2, step 3 (Seasonal Outlook), done 2026-09-25:**
- **Seasonal calls go through `LLMClient`.** `provider.py` became `calls.py`, which holds the profile, each stage's request and the schema conversion, and no SDK code.
  - The profile keeps every setting: the explicit project (never the workstation's), `global`, temperature 1, thinking HIGH, image resolution HIGH, the shared request-type header, the output limits and the phase timeout.
  - `engine.accept` is the call's validator.
- **Two attempts per call on transient errors (D3).** A single timeout, 429 or 503 no longer fails the phase. Refusals such as permission denied still fail at once, and the SDK itself never retries.
- **The analysis record is the call's audit.** `Recorder` implements the tracer's audit hook:
  - it stores each attempt's request before the call and its response before validation, and a failed write fails the call;
  - each call entry gains a `call_id`, and its `attempt` number replaces the constant `attempts: 1`, including in the stored diagnostics;
  - a failed attempt keeps its failure code and error.
- **Shared client additions:**
  - the audit protocol gains `validated` (whose failure fails the call) and `failed` (whose own errors are only logged);
  - a tracer can turn payload capture off, which Seasonal runs do because the audit keeps everything;
  - empty and truncated replies keep their error as the call error's cause, and the empty-reply message names the finish reason;
  - the client's retry pause can be replaced.
- **Errors:** a phase error still shows the analyst the underlying error (for example the provider's message or the validation failure). A truncated reply now reads "LLM response stopped at MAX_TOKENS" and carries the shared failure code.
- **Wire comparison:**
  - the 21 requests of AFY, AMX and ASE are identical;
  - what the analysis record stores is identical apart from the `attempt` numbers and call ids: stored requests, the stored responses' text, raw reply, hashes and token usage, and the call log.
- **Snapshots:** identical, apart from the stored responses of the synthetic fake, which now carry the full diagnostic block that real calls already had. MM requests and MFI requests and snapshots are unchanged.
- **SDK guard:** no module outside `app/shared/llm/` is exempt any more.
- **Verification:**
  - full suite 876 tests, 873 passed, 3 skipped, with no outcome changed;
  - the Seasonal fake now implements the provider interface, and the SDK tests run through the shared provider;
  - added: a transient failure retried within the phase, the retry-or-refusal rule on the wire, no workstation project, the audit hooks and payload capture turned off;
  - every page renders and all modules import.

**Phase 2 cleanup, done 2026-09-25** (Phase 2 is complete):
- `requirements.txt` drops `langchain-google-vertexai`, `google-cloud-aiplatform` and `langchain-core`, which no module imports any more. LangGraph still installs `langchain-core` for itself.
- The image loses 11 packages: the two SDKs and what only they needed (the BigQuery, Resource Manager, Vector Search and IAM clients, `httpx-sse`, `docstring-parser`, `validators`, and pandas' optional `numexpr` and `bottleneck`, which the app does not use).
- **Verification:**
  - An image was built from the staged tree with `git -c core.autocrlf=false archive`, so files keep the repository's LF endings, as in Cloud Build. A plain `git archive` on Windows converts them to CRLF and breaks the prompt-manifest hashes.
  - It passes `pip check` and the suite with the versions installed today (langgraph 1.2.12, langchain-core 1.6.5, google-genai 1.75.0): 846 passed, 30 skipped (the tests that need the confidential datasets).
  - The app starts and answers its health check.

**Phase 3, step 1 (the store), done 2026-09-26:**
- **`app/shared/runs/store.py`** holds Seasonal's store, moved from `seasonal_outlook/storage.py`:
  - one JSON record per run, changed only by `mutate` (a Firestore transaction), with a size limit;
  - content-addressed objects written once, and checksum-verified reads;
  - signed download links and a filtered, ordered history list;
  - memory and Firestore/GCS implementations.
- Seasonal's wording and limits (analysis not found, audit capacity, 800 kB) became parameters, which Seasonal's `storage.py` supplies.
- **`app/shared/cloud.py`:** cached Firestore and Storage clients and `gs://` parsing. The tracer's payload capture uses them instead of building a client for every payload.
- **Verification:**
  - Seasonal snapshots are identical. Since Phase 2 step 3, the comparison masks the stored responses' `started_at` and `duration_seconds`, which change on every run (`.tmp/coherence-baseline/compare_seasonal.py`).
  - Full suite 876 tests, 873 passed, 3 skipped, with no outcome changed. Every page renders and all modules import.

**Phase 3, step 2 (background work), done 2026-09-26:**
- **`app/shared/runs/executor.py`** holds the rules Seasonal had for its phases:
  - `launch` runs work in a daemon thread and logs any error it raises. A job service would replace this one function later.
  - `fenced` applies a change only while the work still holds the run, in one transaction; otherwise it raises `Superseded`, a `Conflict`.
  - `expire_overdue` marks work that missed its deadline as interrupted, checking again inside the transaction.
- Seasonal's launcher, its phase-write guard (`runner._update`) and its deadline check (`Service.get`) now call them, with the same messages and the same order of clock reads.
- **Verification:** Seasonal snapshots identical; full suite 880 tests, 877 passed, 3 skipped, with no outcome changed and 4 new tests of the executor itself.

**Phase 3, step 3 (Market Monitor and MFI runs), done 2026-09-26** (Phase 3 is complete):
- **`app/shared/runs/report_runs.py` replaces `async_runs.py`,** with the same functions, so the routers and the dispatcher changed only where noted here.
- **Storage:**
  - Records live on the shared store: memory by default, or Firestore and GCS with the existing `RUNS_*` settings (same database, collection, bucket and prefix).
  - Every change is a transaction.
  - The result and the artifacts are content-addressed objects, and the result is read only when asked for, never by status polls.
  - Values are stored as plain JSON in memory too, as on Firestore.
- **Lifecycle:**
  - Each record gains `service`, `revision` and a deadline. Each write from the run's work moves the deadline 30 minutes on.
  - A run silent past its deadline reads as `interrupted`. From then on its work can no longer write to it, and a finished run keeps its end state.
  - Completion writes the result, warnings, metadata and status in one transaction.
  - `update_run(live_outputs=…)` merges live-output sections inside that transaction; the four copies of the read-then-write helper now use it.
- **Fail closed:** `RUNS_BACKEND=firestore_gcs` without `RUNS_GCS_URI` now refuses new reports (503) instead of using memory.
- **Old records stay readable:** inline results, `result_gcs_uri`, and artifacts by `storage_uri` or inline. An old record left "running" reads as interrupted.
- **Status "interrupted"** is accepted by both status schemas, and the pages treat it as a final state.
- **Tests:**
  - An autouse fixture gives every test an empty memory run store, whatever the environment configures.
  - `test_async_run_artifacts.py` moved into `test_runs.py`, with lifecycle tests: interruption, late writers, one-write completion, lazy result, live-output merge, and old records.
- **Deferred:**
  - `list_runs(service)`: nothing would call it yet, and on Firestore it needs a composite index. It comes with a history view.
  - The shared launcher for MM and MFI: seven tests run their threads synchronously through `dispatcher.threading`, and Phase 4 rewrites those endpoints anyway.
- **Verification:**
  - Full suite 885 tests, 882 passed, 3 skipped, with no outcome changed. The 13 tests of `async_runs` are replaced by 18 in `test_runs.py`, and one test stub of `create_run` now accepts `service`.
  - Seasonal snapshots identical; every page renders and all modules import.

**Phase 4 spike (D7), 2026-09-26:** can the dispatcher call the FastAPI app in the same process instead of re-implementing it? A throwaway script sent requests through `httpx.ASGITransport`, on an event loop per call, from the 8 worker threads the pages use.
- **Background work:** FastAPI runs `BackgroundTasks` inside the call, so starting a report waited for the whole report. Launches must move to the shared launcher.
- **Files:** a CSV upload and a Word download were identical through both paths.
- **Errors:** the same status codes. Only the wording differs, for an invalid body (the location gains `"body"`) and an unknown route (`Not Found`).
- **Cost:** about 3 ms per call on one thread (the dispatcher: 0.1 ms). With 8 threads the median rose to 18 ms from contention between threads, not from the bridge. There were no leftover threads. Reusing one client did not help, and Starlette's `TestClient` was slower (7 ms). Loading a large result was no slower (MFI Benin, 22 MB: 756 ms against 908 ms).
- **Parity:** every read-only endpoint, a French mock-data Market Monitor result and the MFI Benin result were identical through both paths, 0 differences, and so was the text of their Word exports.
- Each in-process call logs an `httpx` line, which the bridge must filter out.
- **Verdict:** option (b).

**Phase 4, step 1 (launches), done 2026-09-26:**
- Market Monitor and MFI reports start through the shared `executor.launch`, in the routers (instead of `BackgroundTasks`) and in the dispatcher (instead of its own threads).
- The call goes through the module, so tests control it with two fixtures:
  - `immediate_launch` runs the work at once;
  - `deferred_launch` keeps it, and is on by default, so no test races a report thread.
- These replace the fake threads of seven tests.
- **Verification:** full suite 885 tests, 882 passed, 3 skipped, with no outcome changed.

**Phase 4, step 2 (one implementation per endpoint), done 2026-09-26** (Phase 4 is complete):
- **The dispatcher is a bridge:**
  - `dispatch_request` hands every page request to the FastAPI application through `httpx.ASGITransport`, on an event loop of its own. There is no network.
  - Form fields are sent as text and unset ones are left out. An unhandled error becomes a 500 with its message, as before.
  - Its `httpx` log lines are filtered.
  - Its Market Monitor and MFI copies (about 1,760 lines) are gone, and Seasonal no longer takes a separate branch.
- **`app/api.py`** now builds the FastAPI application (it was in `main.py`), so the dispatcher depends on the app package, not on the entry script. `main.py` sets up the environment and logging, then serves it.
- **Before the switch,** a differential run sent the pages' calls through both copies:
  - read-only price-cache calls, a French Market Monitor report (start, status, result, both Word exports, an artifact), an MFI report and a Seasonal flow;
  - everything matched, except the order of MFI's parallel branches, which varies from run to run;
  - two router gaps showed up and are fixed: `/commodities` let an unavailable price cache escape as an unhandled error (now 503), and the MFI start reply could fail on a CSV without a collection period after launching the run.
- **Also fixed on the FastAPI path, which is now the only path (§3.6 item 8):** the synchronous MFI generation reported any `ValueError` raised while drafting as a 400 "CSV validation error". Only an unreadable CSV is a 400 now; a drafting failure is a 500, and a model failure a 502 with its stable public details, as in the Market Monitor.
- **Drift items of §3.4 resolved:** the pages now reach `/cache/refreshes`, and every response they get passes the routers' response models.
- **Tests:**
  - The dispatcher tests now patch the routers (or `data_loader`, whose functions the router imports at call time). They pass unchanged otherwise, so the routers did what the production copies did.
  - Two tests of the dispatcher's own routing and copies became router tests.
  - `test_transports.py` (19 tests) checks that HTTP and in-process calls answer identically: read-only endpoints, errors, invalid input, uploads, Word downloads, form fields, drafting failures, an unhandled error and quiet logs.
- **Verification:** full suite 904 tests, 901 passed, 3 skipped, with no outcome changed; every page renders through the bridge; all modules import.

**Phase 5 tooling:** `.tmp/shared-layer/p5_outputs.py` captures, through the app's API in-process and offline:
- **Word exports:** every part of each Word file (XML as text, images as hashes) and its file name. The inputs are stored results in `p5-fixtures/`:
  - Market Monitor: mock-data runs in English, French and Spanish; synthetic results with two baskets in the three languages, with inline figures, and with a legacy basket;
  - MFI: the Benin, Haiti and Gaza benchmarks.
  Each is exported with four option sets.
- **Report blocks:** the blocks the result endpoints return.
- **News context:** the Market Monitor's news node and MFI's context retrieval over ten scenarios (sources down, bad replies, unmapped or overridden countries, missing keys). Seerist and ReliefWeb are answered at the HTTP layer, so the retrievers' own code runs, and the requests they send are kept.
- **Live outputs:** full runs of both drafters: status metadata, every artifact's bytes and the result.

Two captures of unchanged code are identical (80 files); `p5-before-1` is the baseline.

**Phase 5, step 1 (documents), done 2026-09-26:**
- **`app/shared/documents/`:**
  - `blocks.py` holds `ReportBlock`.
  - `docx.py` renders blocks to Word. The caller passes its look (`WordTheme`: a paragraph style per role, document set-up, layout hints, table renderers) and its words (`WordLabels`). It also holds the Word helpers that themes use.
- **Moved into the drafters:**
  - `market_monitor/report_blocks.py`: the Market Monitor block builder and its basket table. The references heading comes in as a translated label, so shared code no longer imports the Market Monitor's i18n.
  - `mfi_drafter/report_layout.py`: the MFI layout contract and its Word theme (styles, A4 page, header, footer, projected tables). `apply_mfi_layout_contract` is now public, since another module calls it.
- **The caller names the theme:** the renderer used to recognise an MFI document from its blocks. Every MFI result the export accepts (`mfi-light-v1`) carries the layout contract, which the light workflow has applied since its first commit, so no document changes look.
- **Guard:** a test fails if a module in `app/shared` imports a drafter.
- **Verification:**
  - Word exports and report blocks identical for every fixture and option set;
  - MFI snapshots identical;
  - full suite 905 tests, 902 passed, 3 skipped, with no outcome changed;
  - every page renders and all modules import.

**Phase 5, step 2 (live outputs), done 2026-09-26:**
- `live_outputs.py` moves to `app/shared/runs/`, next to the report runs whose live previews and downloads it builds.
- Its table preview was named after DataBridges but knows nothing about it. It stays shared, as `build_table_live_output` and `create_table_artifacts`, instead of moving into the Market Monitor.
- **Verification:**
  - identical: the news context, the live outputs and artifacts of full runs, and the price-rows table with its JSON and CSV downloads (captured directly, since mock-data runs skip that branch);
  - full suite 905 tests, 902 passed, 3 skipped, with no outcome changed;
  - every page renders and all modules import.
- The capture showed defect 11 of §3.6 (API artifact downloads named `….docx`); it is left unchanged.

**Phase 5, step 3 (news context), done 2026-09-26:**
- **`app/shared/context/news.py`** replaces code that the Market Monitor's news node and MFI's context retrieval each had:
  - `gather_context` fetches ReliefWeb and Seerist documents with the drafter's terms and limit (Market Monitor 10, MFI 8) and merges them. It drops a document whose URL (or, without one, its id) was already seen and fills empty content from the title. Its result gives the references, counts, errors and per-source statuses.
  - The drafters still build the retrievers and pass them in, so tests replace them in the drafter's module as before.
  - `live_document_sections` builds each source's live previews and their downloads, which both routers had copied. The Market Monitor passes its translated labels.
- **`app/shared/context/retrievers.py`** (moved) no longer loads `.env` on import; the entry points have done so since Phase 1.
  - `scripts/phase7_second_basket_qa.py` relied on that side effect, and now loads `.env` itself.
  - The Market Monitor wire harness did too: without `.env`, the exchange-rate module has no TradingEconomics key and is skipped. The harness now loads it like an entry point.
- **Verification:**
  - identical: both drafters' context outputs over the ten scenarios, the requests the retrievers send, and the live outputs and artifacts of full runs;
  - Market Monitor wire requests (12) and MFI snapshots unchanged;
  - full suite 905 tests, 902 passed, 3 skipped, with no outcome changed; every page renders and all modules import.

**Phase 5, step 4 (drafter page parts), done 2026-09-26:**
- `streamlit_shared.py` no longer imports a drafter. What belonged to one drafter moves to its `ui.py`, as the Seasonal Outlook already had:
  - `market_monitor/ui.py`: how the basket table appears in the report preview;
  - `mfi_drafter/ui.py`: how its projected tables appear, how its blocks' layout hints are read, and the downloads of its complete analytical tables.
- `render_report_blocks` takes the drafter's table renderers and layout reader, which the pages pass. A table of a kind the drafter did not name still shows as JSON, and a major section after the opening blocks still gets a divider.
- **Verification:**
  - A recording of the Streamlit calls (`.tmp/shared-layer/p5_page_blocks.py`) shows the report preview identical for all 11 fixtures, old renderer against new: titles, headers, dividers, images, tables and notices.
  - Four tests call the moved functions where they now live. The MFI page test replaces the MFI's `ui.py` the way it replaces `streamlit_shared`.
  - Full suite 905 tests, 902 passed, 3 skipped, with no outcome changed; every page renders and all modules import.

**Phase 5, step 5 (following a run), done 2026-09-26:**
- **One mechanism for the Price Bulletin and MFI pages,** in `streamlit_shared.py`, on the fragment polling the Seasonal Outlook page already used:
  - `start_run` starts a report and makes it the page's run: the session follows it and the page URL names it (`?mm_run=`, `?mfi_run=`).
  - `follow_run` refreshes the run's status every 2 seconds in a fragment. When the run ends, it loads the result if there is one, hands it to the page once and reruns the page.
- **What changes for users:**
  - The Price Bulletin page no longer blocks while a report runs, for up to an hour. Before, any click during a run stopped the loop, and the page lost the run.
  - A failed or interrupted report keeps its final status on the page. The Price Bulletin page used to clear it as soon as the run ended.
  - A reloaded or shared Price Bulletin URL picks the run up again, as the MFI page already did.
  - The MFI page loses its second polling path (a 10-second fragment for reloads). Its phase table is shown with the status, and its errors appear as errors rather than warnings.
  - A completed report shows the report, as the Price Bulletin page already did. The MFI page used to show the final status above it until the next click.
- The Seasonal Outlook page keeps its own fragment, since it follows an analysis rather than a report run.
- **Verification:**
  - `tests/test_run_following.py` (5 tests) runs the real `start_run` and `follow_run` in AppTest: polling, the hand-over of the result, failed runs, a run named in the URL, a failed status request that the next refresh retries, and a new run replacing an old failure.
  - The page tests of both drafters now run the real `start_run` and `follow_run` over their fake backends; their assertions are unchanged, except for the MFI error shown as an error.
  - In a browser, the app ran offline (`.tmp/shared-layer/p5_offline_app.py`: fake model with pauses, fake news sources, the Price Bulletin page's price data from its test backend):
    - a Price Bulletin report ran to its report, with live progress;
    - a report that failed kept its error on the page;
    - a click during a run did not lose the run;
    - an MFI run opened by its URL was followed to its report.
  - Full suite 909 tests, 906 passed, 3 skipped, with no outcome changed; every page renders.

---

## 6. Decisions

Taken on 25 September 2026.

| # | Question | Decision |
|---|---|---|
| D1 | SDK behind the client | `google-genai` for all three drafters (option A below). |
| D2 | Models and parameters | Unchanged per drafter (MM stays on Gemini 2.5 Pro, temperature 0). Model changes are a separate, evaluated decision. |
| D3 | Automatic retries | Transient errors only, each attempt recorded. MM: 2 attempts (as today, but no longer on permission or invalid-argument errors). MFI: unchanged. Seasonal: 2 attempts per call on transient errors, so a single 503 no longer forces the analyst to rerun the whole phase. |
| D4 | Payload capture for MFI | Opt-in, off by default, with the same controls as MM. |
| D5 | MM truncated responses | Fail the call, as MFI and Seasonal do. |
| D6 | Runs | MM and MFI runs move to the shared store; a common record shape is decided later. |
| D7 | Duplicated endpoints | Included. Option (b) of §4.5 after a spike, otherwise (a). |
| D8 | Existing defects (§3.6) | 1–4 and 9 are fixed on `fix/post-phase7` and ship right after Phase 7 acceptance, before this refactor. |
| D9 | Timing | Work starts now on `refactor/shared-layer`. Nothing is merged or deployed before Phase 7 acceptance and the release of D8. |

**What D1 changed** (A was chosen). For analysts, nothing: the same models, prompts and settings either way, and in both cases every call goes through the same client with the same tracing, retries and errors. The difference is inside the client.

| | A: `google-genai` everywhere (recommended) | B: LangChain kept for MM and MFI |
|---|---|---|
| Implementations inside the client | one | two (LangChain for MM and MFI, `google-genai` for Seasonal) |
| MM and MFI requests | sent through new code; proven unchanged by request snapshots and one live run each | keep today's exact path |
| MFI token counting and schema check | public `google-genai` calls | still LangChain internals, which any upgrade can break |
| Dependencies | `langchain-google-vertexai` and `google-cloud-aiplatform` can go (LangGraph stays) | both stay |
| Converters, response formats, error families | one of each | two of each, permanently |
| New features (for example thinking settings for MM) | implemented once | implemented twice |

A carries a one-off migration risk, and snapshots measure it. B carries a permanent maintenance cost.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Switching SDK changes what the model receives | Transport-level snapshots of exact requests per drafter; migrate one drafter at a time; MM prompt text and parameters compared byte for byte |
| The two schema converters produce different MFI or Seasonal schemas | Golden files of the generated schemas; the unified converter must reproduce both |
| Stored records stop loading (MM/MFI results, `async_runs` documents, Seasonal calls) | Additive changes only; readers accept the old trace schema; no Seasonal workflow-revision bump |
| Retry changes affect cost and latency | Every attempt is recorded; D3 keeps today's attempt counts except Seasonal |
| In-process FastAPI calls misbehave in Streamlit threads (D7) | Spike first; fall back to option (a) |
| Scope creep | Phases are independent; 1–3 deliver what was asked (LLM, observability, runs) |
| Overlap with the release being verified | Separate branch; nothing is merged or deployed before Phase 7 acceptance and the D8 release |
