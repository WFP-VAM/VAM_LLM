# Cloud provider boundary

## Scope and operating backend

`app/services/` owns workflows, prompts, scientific Pydantic contracts, application validation,
analyst confirmation and report composition. It must not import cloud SDKs or concrete cloud
adapters, select a model, construct provider schemas, interpret cloud URIs or read provider
fields inside `LLMResponse.raw`.

`app/shared/` owns LLM configuration and adapters, token measurement, object resolution,
execution record persistence and deployment aliases. Vertex AI, Firestore and GCS remain
the production backend. No AWS adapter or model has been selected or implemented.
Dependencies, credentials, environment variables and deployment configuration remain separate
operational concerns; the boundary applies to application source code.

Graphs, scientific methodology, prompts and human review are unchanged. Execution remains
in daemon threads in the application process. This change adds neither distributed jobs nor
restart/checkpoint recovery.

## LLM contract

Use `create_llm_client(service, ...)` from `app.shared.llm`. A client has immutable resolved
profiles and a measurement cache scoped to its lifetime. An MFI run creates one client;
each Seasonal operation creates one client, including an analyst-initiated retry. The Market
Monitor shares one client through its run tracer. Configure adapters only in `shared/llm/`.
Tests can supply `provider`, `profiles` and/or `provider_registry`; no SDK import is needed for
a replacement provider. Informational endpoints use `describe_llm_config` without credential
discovery. Execution freezes the resolved project, including ADC when necessary. Missing configuration is
frozen as missing: a node that skips inference can still finish, while any actual model call
fails with a configuration error without consulting later environment changes.

`LLMRequest` supplies operation/node identifiers, text/system instructions, `FilePart` objects,
standard JSON Schema, workflow timeout and retry/repair links. Services do not set model
names, headers, temperature, image resolution or reasoning parameters.

The client supports `generate`, `generate_json`, `generate_text`, `measure` and `describe`.
`describe()` returns actual non-secret profile settings, selected provider, dependency versions,
transport attempts and SDK retry policy. It excludes credentials and headers. MFI records this
snapshot in its runtime/effective contract; Seasonal records it on the operation and retains
the effective provider, requested/reported model, parameters and usage for every attempt.

| Shared profile | Operations | Initial GCP settings |
| --- | --- | --- |
| MFI `text` | Draft/review/correction | Gemini 3.1 Pro preview, global, temperature 1, 65,536 output, 600 seconds |
| MFI `summary` | Executive summary | Same model/settings, 180 seconds |
| Seasonal `evidence` | Extraction/review/refinement/feedback | `SEASONAL_MODEL`, 32,768 output, HIGH reasoning/image resolution |
| Seasonal `report` | Draft/report_review/redraft | `SEASONAL_MODEL`, 65,536 output, same initial Vertex parameters |
| Market Monitor `text` | Price Bulletin operations | Existing `LLM_*` / `VERTEX_LOCATION` configuration, temperature 0 |

Routing is based on existing operation identifiers in `shared/llm/profiles.py`. Seasonal's two
profiles can use different providers/models. Informational `model` and `location` are `null`
when the profiles disagree; `profiles` always describes them separately. A new Seasonal
operation re-resolves environment-backed model settings; a running operation keeps its snapshot.
The analysis store remains attached to the store that opened the service.

`LLMResponse.outcome` is required: `completed`, `truncated`, `blocked`, or `unknown`.
`finish_reason` is optional native diagnostic data and must never drive service decisions.
`usage` contains `prompt_tokens`, `candidate_tokens`, `thought_tokens`, `total_tokens`, each
`None` when unavailable. `raw` is opaque JSON-compatible diagnostic data. Seasonal archives
it but does not inspect it. Only completed replies enter its scientific validators. MFI can
retain complete sections from a truncated reply and repair the missing sections.

Adapters translate SDK failures to `ProviderError(kind=...)`: `configuration`, `request`,
`authentication` or `transport`; only explicitly transient transport failures are retried.
This applies to inference and token counting. Missing exact token counting is an incompatibility,
not an invitation to estimate. Configuration and authentication errors never cause content repair.
MFI retains at most two runtime attempts with one client attempt. Seasonal uses at most two
transport attempts. The Vertex SDK itself has one attempt (zero retries).

## Schemas and budgets

Pydantic models and local validation remain in services. `shared/llm/vertex_schema.py` expands
references and compiles Vertex schemas. MFI property order is alphabetical; Seasonal order
follows declaration order. Titles/defaults, extra-key prohibition and string length constraints
are omitted from transport but still checked by the original application validators. Unsupported
structural constructs, recursive/external references and invalid types fail before inference.
Compiled schemas are cached by source schema, model, conversion policy and compiler version
inside the adapter; every caller receives an independent copy.

Tracing records separate `contract_schema_hash` and `transport_schema_hash` values. The
former hashes the full application contract, the latter the compiled provider contract.

`measure(request)` returns characters, exact input tokens, effective limits and fingerprint.
Vertex preserves MFI's previous compact JSON character measurement and the 1,200,000
character / 250,000 input-token limits. Counts use a 30-second timeout and at most two
transient attempts. The cache includes effective configuration, model, contract, provider
representation, system instructions and contents. Workflow/artifact identifiers do not invalidate
equivalent requests. Concurrent identical measurements perform one count. MFI's counter
increments for real count attempts only; splitting oversized section groups remains in MFI.

## Object and record storage

`shared/runs/store.py` defines `RunStore` and `ObjectRef`; `shared/runs/factory.py` selects the
backend by service. Required methods are `put`, `read`, `put_json`, `json`, `get`, `list`,
`mutate`, `download_link`. Updates to records are atomic; mutation callbacks must not invoke
models or write objects because durable transactions may retry them.

An object reference is JSON data with exactly these fields:

```json
{"namespace":"seasonal-outlook","key":"seasonal-outlook/run/<sha256>",
 "sha256":"<64 lowercase hexadecimal characters>","size":12345,"mime":"image/png"}
```

The namespace is logical, never a bucket, account or provider URI. Keys are content addressed.
Readers check namespace, path, size and SHA-256. GCS writes use `if_generation_match=0`
and verify an already existing object's checksum; model resolution verifies original bytes
before supplying its GCS URI internally. Image references, figure identifiers and notes preserve
order. The inference path never resizes, recompresses or converts originals. The initial adapter
accepts PNG/JPEG/WebP up to 30 MB each, at most 12 files and 50 MB total; incompatible input
is rejected explicitly. Word-only conversions for the appendix remain separate.

Seasonal always requires durable production storage, explicit project/bucket and the configured
download signer. Its record limit remains 800,000 encoded bytes. Tests inject memory storage.
Market Monitor/MFI retain the explicit/unconfigured local memory option. A configured durable
backend never silently falls back to memory, including when opening/writing it fails. Unknown
backend identifiers are configuration errors.

`download_link` returns `{url, expires_in}` from one signing policy; APIs do not invent a TTL.
The initial signing TTL is 600 seconds. The shared inline-delivery limit is 20,000,000 bytes.
Application revision checks, idempotence, deadlines and protection from late writers are retained.

## Existing configuration

No new public environment format is introduced. `shared/config.py` also resolves the existing
Cloud Run deployment metadata aliases `K_REVISION` / `K_SERVICE` and the `REVISION_ID`
fallback; services retain only their release gates and neutral metadata. Shared code continues to interpret
`VERTEX_PROJECT_ID`, the existing project aliases/ADC, `LLM_*`, `VERTEX_LOCATION`,
`SEASONAL_*`, `RUNS_BACKEND`, `RUNS_GCS_URI`, `RUNS_FIRESTORE_DATABASE`,
`RUNS_FIRESTORE_COLLECTION`, `LLM_TRACE_PAYLOADS` and `LLM_TRACE_GCS_URI`.
Seasonal does not inherit a workstation project. Its initial Vertex location remains `global`.

Price Cache exposes only `sqlite` or `postgres` to its implementation. Shared
`database_backend_alias` translates the existing `cloud_sql_postgres` / `cloud-sql-postgres`
aliases for configuration and migrations. The unused `PRICE_CACHE_GCP_PROJECT`,
`PRICE_CACHE_GCP_REGION` and `PRICE_CACHE_GCP_CLOUD_SQL_INSTANCE` settings were removed.
PostgreSQL pooling, timeouts and keepalive behavior remain unchanged.

## Record discontinuity and release

- Shared report records: `record_version=3`.
- Seasonal analyses: unchanged `workflow_revision=seasonal-graph-v1`, new `runtime_contract_version=2`.
- Call/run traces: `trace_schema_version=2.0`, private payload path `v2`.
- Scientific methodology, evidence/report schemas and prompt/workflow versions are unchanged.

Only new executions are supported. Older or unknown record versions produce HTTP **410** for
opening, status/result retrieval, retry, export and download as applicable, in both FastAPI and
the Streamlit dispatcher. Old Seasonal records can still appear in history but cannot be opened.
Reads do not migrate, rewrite or delete them. Previous trace formats are not silently interpreted
as new records. This policy supersedes historical additive-compatibility instructions in older specs.

Before switching application versions, stop new submissions and let all active reports/phases
finish. Do not transfer active executions between versions. Retain previous data unchanged;
analysts must create fresh analyses after the switch. Rollback requires the previous application
for its previous records; neither version should reinterpret the other's records. No deployment
or data migration is performed by this source refactor.

## Verification and future AWS integration

Application tests cover workflow behavior independently of Google serialization. The schema
fixture `provider_schema_baseline.json` was derived from the original converters at commit
`0c143c4`, and fixes the complete MFI and Seasonal transport-contract hashes. `test_vertex_*.py`
uses the real installed SDK with simulated HTTP; `test_cloud_store.py` uses storage/transaction
doubles. `test_cloud_neutral.py` checks architectural imports/literals, budget caching, normalized
outcomes, multi-provider Seasonal routing, audit snapshots, original images, download policy,
record versions and transport parity. It launches a fresh interpreter with Google/cloud SDK
imports blocked and runs full MFI and Seasonal workflows, exports and Market Monitor model nodes
with non-Gemini profiles and normalized test replies. Scientific regression tests remain in place.

Run targeted tests and then `python -m pytest tests`. Live GCP validation, if desired, must use
a dedicated test environment; offline tests do not verify IAM, network access or deployed models.

All future application integration points are in `shared/`:

1. Add an LLM adapter implementing normalized generation, exact measurement/counting, schema
   conversion, object resolution, capability checks and SDK error normalization; select it in
   `llm/client.py` and map profiles in `llm/profiles.py`.
2. Add a `RunStore` implementation with immutable objects, conditional/atomic record updates,
   namespace/checksum validation and actual signed-download TTL; select it in `runs/factory.py`.
3. Update shared configuration/credential resolution and optional private trace storage transport
   (`llm/tracing.py`, `cloud.py`). Do not read native SDK fields in services.
4. Validate the chosen AWS/model combination against these contracts and the scientific fixtures.
   Endpoint choice (Bedrock or other hosting), model quality, regional capabilities, IAM and
   deployment/dependency installation belong to that subsequent migration phase.
