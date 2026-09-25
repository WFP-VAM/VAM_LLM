# Seasonal Outlook Drafter in VAM LLM

Implementation reference, 18 September 2026. This document supersedes the earlier Kimi/GLM feasibility assessment. The application uses **Gemini 3.1 Pro through Vertex for every Seasonal model call**. The local experimental prototype is unchanged.

Activation updated on 21 September 2026 at the user's request: Seasonal is **enabled by default** in application settings and deployment examples. An explicit `SEASONAL_DRAFTER_ENABLED=false` remains an operational rollback. This replaces the original disabled-by-default rollout decision. Enabling the flag does not provision infrastructure: new processing still requires explicit company configuration and the resources below. Updating only the current app image is insufficient to make Seasonal operational. Live company GCP and scientific acceptance checks remain outstanding.

Execution updated on 25 September 2026 by the coherence refactor (`coherence_refactor_plan.md`, Phase 4): the phases run as a LangGraph graph in a background thread of the web service. The Cloud Run Job, the worker lease and the checkpoint/resume layer were removed; a failed phase is retried as a whole.

## Application and scientific behavior

`app/services/seasonal_outlook` owns input preparation, contracts, prompts, provider, persistence, execution and exports. Both the `/seasonal-outlook` FastAPI router and the Streamlit dispatcher call `api.handle` and the same `Service`. `pages/5_Seasonal_Outlook_Drafter.py` is reachable from the home page and uses the existing branding and assistance links.

The Input package tab accepts all maps in one selection and saves them with one **Save selected maps** action. Category, issue date and notes are optional, in collapsed per-map panels. Explicit filename prefixes can suggest a category; Gemini identifies unclassified products during image extraction. Calendar and checklist details are also collapsible. The UI validates the entire selection, including already saved maps, before sending the existing individual upload requests with successive expected revisions. An interrupted batch retains the selection and requires another explicit save; images already present are recognized by content hash and skipped, including when a persistence acknowledgement was lost. Extraction is unavailable while selected maps remain unsaved or invalid. This change requires only an application image update, with no new cloud resources or API routes.

The explicit stage graph is extraction → visual review → refinement → analyst pause. Feedback produces another evidence version and another pause. Confirming the current version reserves drafting → textual review → complete redraft. Each stage constructs fresh input. Report requests contain only the confirmed evidence, calendar, scientific/editorial rules and, where needed, the first draft and textual review. They contain no images or original analyst comments. Using one model does not make its reviews independent verification.

In code this is one LangGraph `StateGraph` (`graph.py`) with an entry point per phase: `extract` (extraction, review, refinement), `feedback` (feedback) and `report` (draft, report review, redraft, then the export node). Its edges are generated from `engine.CHAINS`, and it is compiled without a checkpointer. Each phase is one invocation, started by `runner.run_phase`; the analyst pause is the gap between two invocations and lives in the analysis record, not in the graph.

The port preserves prototype scientific rules and rule IDs, identifier normalization, dynamically constrained references, calendar modes, observed/forecast/mixed distinctions, editorial instructions and AFY scope. The bundled calendar includes the AFY Eastern Africa/Yemen extension separately from Horn of Africa. Rule metadata retains original experimental provenance; Kimi/GLM provider code and local filesystem workers are not imported. No runtime resource is read from the prototype or a Windows path.

The operational contracts are `seasonal_evidence_v1` and `seasonal_report_v1`. Python allocates full evidence identifiers, validates map inventories, source ownership, verbatim analyst quotations, issue resolutions, citations and eligible seasons. A model response with an issue date after the cutoff or a reversed validity interval fails validation. These checks cannot establish scientific truth: an analyst still reviews maps and prose.

## Vertex adapter

Google Gen AI SDK is instantiated with `vertexai=True`, the explicit Seasonal project, ADC service identity and `global`. It never uses a Together key or the other workflows' model configuration. Default model: `gemini-3.1-pro-preview`; temperature 1.0; thinking HIGH; image resolution HIGH; output limits 32,768 evidence / 65,536 report tokens. Requests have one total attempt and 600-second timeouts. A retry of a failed operation accepts 600, 1,200 or 1,800 seconds.

Original images are immutable GCS objects supplied as `gs://` URI parts. The adapter resolves Pydantic references, converts constant values into enums, keeps Gemini-supported schema fields and sends the result in API configuration. It does not duplicate schemas in prompts. The original Pydantic model and semantic validators remain authoritative locally.

Returned content, finish reason, actual/requested model, prompt/contract versions and hashes, response identifiers, token usage, duration, errors and configured limits are recorded per call. Empty, blocked, truncated, malformed and semantically invalid responses stop the operation. Returned responses are saved before validation; request/response bodies remain in the private Seasonal bucket rather than ordinary application logs. No hidden reasoning transcript is requested.

References: [model limits and Preview status](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-pro), [Gemini 3 guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/get-started-with-gemini-3), [structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output).

## Persistence and concurrency

Firestore holds one analysis record per run in `seasonal_outlook_runs`; GCS holds immutable content-addressed maps, input snapshots, each operation's input and final output, requests, responses, evidence versions and exports. The application uploads an object before its reference is committed in a Firestore transaction. A crash can leave an unreferenced object; it cannot publish a reference to an object whose upload did not finish. No automatic deletion policy is applied to analysis history. The record is what the analyst's pause needs: inputs, immutable evidence versions, comments, the confirmation of one exact version, and each operation's calls and output. It is not an execution checkpoint.

All modifying API requests include `request_id` and `expected_revision`; create requires revision 0. Reusing the same key with identical input returns the same operation without starting the phase again. Reusing it for different input or editing a stale revision returns 409. Inputs freeze once extraction starts. Corrections to region, date, original maps or metadata require a new analysis.

One operation can own a run at a time. Different analyses can execute concurrently. Once its reservation is stored, the service starts the phase in a background thread of the web process; concurrent copies of one request store a single reservation and start it once. If the thread cannot start, the operation fails at once. Each operation has a deadline (stages × per-call timeout + 10 minutes); an operation still active after its deadline — for example because the instance stopped — is marked interrupted when the analysis is read. Every write of a running phase checks that its operation is still the active one, so a late thread cannot publish results or overwrite a successor's state.

Nothing is checkpointed between stages. A phase publishes its evidence versions only when it succeeds; extraction publishes V1 and V2 together. A failed or interrupted phase keeps only its calls, saved responses and error. The analyst retries the latest operation, which reruns the whole phase from the same stored inputs. Evidence-version checks reject a stale retry; a report retry additionally requires the exact confirmed evidence hash. A retry may repeat a remote call whose outcome was uncertain; model inference is not claimed to execute exactly once.

Analyses created before the refactor remain in the history list but can no longer be opened (410).

Manifests have a conservative 800 KB transaction limit and 500 modifying request limit; large bodies live in GCS. An analysis reaching this audit limit requires a new run. Shared access follows the app's existing authenticated deployment boundary; the service does not implement a new individual-user access model. Keep the app/API behind that boundary, since users can read each other's runs and generate signed download URLs by design.

## API surface

All paths below are relative to `/seasonal-outlook`. Errors use 400 for input/contract errors, 404 for missing resources, 409 for stale/concurrent modifications, 410 for analyses created before the refactor and 503 for disabled/unconfigured processing.

| Method | Path | Behavior |
|---|---|---|
| GET | `/info` | Configuration status, model, regions, categories and limits; no inference |
| POST | `/runs` | `request_id`, `expected_revision: 0`, `region_id`, ISO `report_date`, optional `notes` |
| GET | `/runs` | Shared history; optional `region_id`, `report_date`, `status`, `limit`, `before` timestamp |
| GET | `/runs/{id}` | Analysis record: versions, operations and availability of exports |
| POST | `/runs/{id}/maps` | Multipart: one `file`, request ID, expected revision, `product`, optional `issue_date`, `note` |
| GET | `/runs/{id}/input` | Validated input and calendar/checklist limitations |
| GET | `/runs/{id}/maps/{index}` | Original image; zero-based index |
| POST | `/runs/{id}/extract` | Start the extraction phase |
| POST | `/runs/{id}/feedback` | Current `version_id` and literal `comments` |
| POST | `/runs/{id}/confirm` | Current `version_id`, `confirmed: true`; start the report phase |
| POST | `/runs/{id}/retry` | `operation_id` of the latest operation, if it failed or was interrupted; optional timeout |
| GET | `/runs/{id}/versions[/{version}]` | Version inventory or complete evidence |
| GET | `/runs/{id}/operations[/{operation}]` | Operations oldest first, or one operation with its phase output |
| GET | `/runs/{id}/input-package` | Signed URL for frozen original input ZIP, available after extraction starts |
| GET | `/runs/{id}/download-link/{name}` | Signed export URL, 10-minute lifetime; optional prior `operation_id` |
| GET | `/runs/{id}/artifacts/{name}` | Small export bytes; exports over 20 MB require a signed URL |

Input bounds: static PNG/JPEG/WebP, 1–12 maps, 30,000,000 bytes per image, 50,000,000 bytes combined, 45,000,000 pixels per image. Duplicate bytes are rejected. The UI uploads individual maps, rather than one oversized multipart package.

The three tabs show evidence beside the original map, version comparisons, comments/decisions, reports/downloads and inputs/checklists; **Operations and review decisions** lists the operations and offers **Retry failed operation** for the latest one when it failed or was interrupted. Region starts unselected. URLs carry `seasonal_run`. Polling is confined to an active-operation fragment; waiting for analyst review leaves no polling loop. Rendering, tab changes and refreshes never invoke a model. Explicit confirmation is required in the UI and enforced independently by the backend.

Exports include Word with/without map appendix and an artifact ZIP with rules, input, original images, evidence versions, review decisions, requests, responses and each earlier operation's output. Original maps are preserved byte-for-byte in ZIP/GCS. Word appendix rasters are bounded to 300 dpi at page size (1,860 × 1,800 pixels); WebP is converted to PNG only in that appendix to ensure compatibility and bound memory. Prior reports remain available under their own operation after new feedback invalidates the current report.

## Company deployment

The user will configure the company project manually in the browser console. Follow [the console setup guide](../deploy/seasonal-outlook/CONSOLE_SETUP.md) for that deployment, including its section on upgrading a configuration that still has the Job. Terraform below is an optional equivalent reference, not a required step. Do not apply Terraform to manually created resources without importing them first.

Use `deploy/seasonal-outlook/main.tf` with explicit company parameters. This module is additive: it creates the Seasonal bucket, the download-link signer identity, IAM grants and history indexes; it references the existing Firestore Native database and app service account. It does not recreate the app, database, authentication or other workflows. Import pre-existing Seasonal resources into the Terraform state before applying, if necessary. Store Terraform state under the company's normal infrastructure controls.

1. Build the app image. The existing Dockerfile includes the code and bundled resources. Run the offline tests and the Linux container check below.
2. Copy `terraform.tfvars.example` to a private values file. Supply company project, region, separate Seasonal bucket name, signer account ID, existing app account email and existing database. No personal gcloud defaults are consulted by application code.
3. Authenticate deployment tooling using the company identity and project. Run `terraform init`, `terraform validate`, `terraform plan -out=seasonal.plan`; review the changes, then apply the approved plan. No Terraform commands have been applied as part of the local implementation.
4. Wait for the IAM grants and Firestore indexes to be ready. Add `terraform output -json app_environment` entries to the existing app service with the normal deployment pipeline, preserving all unrelated environment settings. The requested active deployment uses `SEASONAL_DRAFTER_ENABLED=true`; replace an existing explicit `false` if present. A source-code default cannot override an existing environment variable.
5. The phases run in the app service: keep **CPU always allocated** (Market Monitor and MFI need it too), give the service enough memory for Word/ZIP exports with twelve maps (the former Job had 4 GiB), and prefer at least one minimum instance.
6. Run the acceptance checklist below against the configured company deployment. Application and Terraform defaults now enable processing, as requested; use an explicit `false` for a staging hold or rollback. Setting the feature flag false blocks new operations while preserving history and downloads; phases already running may finish.

IAM grants: the app identity gets `aiplatform.user` and `datastore.user` on the project, `storage.objectUser` on the Seasonal bucket, and `iam.serviceAccounts.signBlob` **on the signer identity**; the signer identity gets `storage.objectViewer` on the bucket, since a signed URL grants the signer's access. Firestore and Vertex predefined roles are project grants; confirm these fit company policy. No service-account key files are required. The deployer separately needs authority to build/pull the image, create these resources/roles and bind IAM.

The SDK explicitly sends `X-Vertex-AI-LLM-Request-Type: shared` for online PayGo; it does not request Priority/Flex tiers or provision dedicated serving/GPUs. Availability, quotas, Preview approval, regional policy and billing must be checked in the company project.

Local verification commands:

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_seasonal_outlook.py -q
.\venv\Scripts\python.exe -m pytest tests\test_seasonal_upload_ui.py -q
.\venv\Scripts\python.exe -m pytest tests -q
```

Linux/container verification when a Docker daemon is available:

```sh
docker build -t vam-llm-seasonal:test .
docker run --rm --entrypoint sh vam-llm-seasonal:test -c 'pip install pytest httpx && python -m pytest tests/test_seasonal_outlook.py -q'
```

## GCP and analyst release gate

Live GCP acceptance is deliberately separate from offline synthetic fixtures. In the company staging deployment:

- Verify enabled APIs, database/index readiness, private bucket access, signing permission and the app identity's Vertex access to the exact Preview model. Confirm no API-key or Together dependency.
- Upload real AFY, AMX and ASE input packages manually. Check the region remains explicit and AFY does not acquire a Horn of Africa section. Validate issue dates, modes and product classifications with the analyst.
- Complete extraction, visual review and refinement. Compare evidence with original maps; record actual model, prompt hashes, latency and token counts. Historical outputs are comparison material, not ground truth.
- Submit at least one analyst feedback cycle, inspect applied/unverifiable decisions, compare versions and explicitly confirm the latest version. Validate report citations, geography, observed/mixed/forecast language and omissions scientifically.
- Repeat an action with the same request ID and stale revision; verify one reserved operation. Have two browsers modify the same revision; verify a 409 for the losing change. Submit the same action twice at once; verify only one phase starts. Run two different analyses concurrently.
- Close the browser while a phase runs, reopen the shared URL from another user/instance, and verify progress and artifacts persist. Force a phase to fail, or stop the instance during a phase and wait for its deadline, then retry the operation. Verify the whole phase reruns from the same inputs and that the failed attempt published no evidence version.
- Validate empty/truncated/blocked responses using controlled test doubles or a staging test harness, not invented scientific examples sent to production. Verify errors retain saved responses and require an explicit retry.
- Open Word with/without maps and ZIP exports, including a large package retrieved through a signed GCS URL. Verify an expired URL is rejected and a new one can be requested. Disable processing and confirm shared history/downloads remain available.
- Record analyst acceptance and GCP observations for the active deployment. Activation alone is not evidence that these checks passed. Prototype local-run import and automatic map retrieval remain out of scope.

## Local verification record

Coherence refactor, 25 September 2026: **38 Seasonal tests passed**, including phase retry from the stored inputs, deadline expiry, a late thread unable to overwrite a newer operation, report retry requiring the confirmed evidence hash, a phase that cannot start, records of the previous workflow (410), the real background thread and the retry control on page 5. A snapshot comparison showed the phase graph reproducing the former Job worker exactly on synthetic AFY, AMX and ASE flows: every model request, evidence version, operation output, Word text and ZIP content. No cloud calls were made.

Batch-upload update on 21 September 2026: **34 Seasonal tests passed** (48.36 seconds), including 11 new UI/upload cases. Covered optional per-map metadata, sequential revision checks, interruptions before persistence, lost acknowledgements after persistence, concurrent analysts, duplicate/corrupt/animated/oversized images, package limits, and reopening saved selections without uploading again. A subsequent focused run of all three interruption scenarios passed with an additional assertion that unsaved map notes survive recovery. These checks use the actual application service with an in-memory store and no cloud calls or live inference.

Rechecked on 21 September 2026 after changing the activation default: **23 Seasonal tests passed** (30.45 seconds), including enabled-by-default behavior, explicit disablement and prevention of new runs without company configuration. No cloud calls were made.

Verified on 18 September 2026 (Job-based version), without company GCP credentials or live model inference:

- Full existing application suite with the initial Seasonal tests: **1,265 passed, 4 skipped, 4 expected failures**, 883.53 seconds.
- Final Seasonal suite: 22 tests, including AFY/AMX/ASE synthetic flows, feedback, exact-version confirmation, stale actions, duplicate dispatch, expired-worker fencing, selective resume, invalid responses, input limits, API/dispatcher parity and Streamlit refresh behavior. The two final SDK tests exercise real Google Gen AI serialization through an in-process HTTP test transport, including all six phases and zero retries on HTTP 503.
- New Python modules compile, and `git diff --check` passes. Terraform HCL parses successfully (13 resource declarations); provider-level `terraform validate/plan` is still a company deployment check.
- Word exports with/without maps were opened and rendered by Microsoft Word; rendered pages were visually inspected. ZIP contents and image appendix presence were checked programmatically. LibreOffice is unavailable on this workstation, so the Word rendering was used for visual QA. The fixtures are synthetic, not scientific validation.
- Actual Streamlit page checked in the browser with processing disabled; corrected local contrast and wrapped the existing assistance banner without changing shared styling.
- Linux container execution was not run: Docker CLI exists but its daemon is not running. Docker commands above remain a release prerequisite.
- No Terraform apply, company GCP run, quota check, model-quality acceptance or production enablement was performed. These require the explicit company deployment parameters and analyst acceptance described above.
