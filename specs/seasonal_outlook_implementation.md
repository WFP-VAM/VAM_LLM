# Seasonal Outlook Drafter in VAM LLM

Implementation reference, 18 September 2026. This document supersedes the earlier Kimi/GLM feasibility assessment. The application uses **Gemini 3.1 Pro through Vertex for every Seasonal model call**. The local experimental prototype is unchanged.

Activation updated on 21 September 2026 at the user's request: Seasonal is **enabled by default** in application settings and deployment examples. An explicit `SEASONAL_DRAFTER_ENABLED=false` remains an operational rollback. This replaces the original disabled-by-default rollout decision. Enabling the flag does not provision infrastructure: new processing still requires explicit company configuration and the resources below. Updating only the current app image is insufficient to make Seasonal operational. Live company GCP and scientific acceptance checks remain outstanding.

## Application and scientific behavior

`app/services/seasonal_outlook` owns input preparation, contracts, prompts, provider, persistence, execution and exports. Both the `/seasonal-outlook` FastAPI router and the Streamlit dispatcher call `api.handle` and the same `Service`. `pages/5_Seasonal_Outlook_Drafter.py` is reachable from the home page and uses the existing branding and assistance links.

The explicit stage graph is extraction → visual review → refinement → analyst pause. Feedback produces another evidence version and another pause. Confirming the current version reserves drafting → textual review → complete redraft. Each stage constructs fresh input. Report requests contain only the confirmed evidence, calendar, scientific/editorial rules and, where needed, the first draft and textual review. They contain no images or original analyst comments. Using one model does not make its reviews independent verification.

The port preserves prototype scientific rules and rule IDs, identifier normalization, dynamically constrained references, calendar modes, observed/forecast/mixed distinctions, editorial instructions and AFY scope. The bundled calendar includes the AFY Eastern Africa/Yemen extension separately from Horn of Africa. Rule metadata retains original experimental provenance; Kimi/GLM provider code and local filesystem workers are not imported. No runtime resource is read from the prototype or a Windows path.

The operational contracts are `seasonal_evidence_v1` and `seasonal_report_v1`. Python allocates full evidence identifiers, validates map inventories, source ownership, verbatim analyst quotations, issue resolutions, citations and eligible seasons. A model response with an issue date after the cutoff or a reversed validity interval fails validation. These checks cannot establish scientific truth: an analyst still reviews maps and prose.

## Vertex adapter

Google Gen AI SDK is instantiated with `vertexai=True`, the explicit Seasonal project, ADC service identity and `global`. It never uses a Together key or the other workflows' model configuration. Default model: `gemini-3.1-pro-preview`; temperature 1.0; thinking HIGH; image resolution HIGH; output limits 32,768 evidence / 65,536 report tokens. Requests have one total attempt and 600-second timeouts. Explicit resumes accept 600, 1,200 or 1,800 seconds.

Original images are immutable GCS objects supplied as `gs://` URI parts. The adapter resolves Pydantic references, converts constant values into enums, keeps Gemini-supported schema fields and sends the result in API configuration. It does not duplicate schemas in prompts. The original Pydantic model and semantic validators remain authoritative locally.

Returned content, finish reason, actual/requested model, prompt/contract versions and hashes, response identifiers, token usage, duration, errors and configured limits are recorded per call. Empty, blocked, truncated, malformed and semantically invalid responses stop the operation. Returned responses are saved before validation; request/response bodies remain in the private Seasonal bucket rather than ordinary application logs. No hidden reasoning transcript is requested.

References: [model limits and Preview status](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-pro), [Gemini 3 guidance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/start/get-started-with-gemini-3), [structured output](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/control-generated-output).

## Persistence and concurrency

Firestore holds one manifest per run in `seasonal_outlook_runs`; GCS holds immutable content-addressed maps, input snapshots, requests, responses, checkpoints, evidence versions and exports. The application uploads an object before its reference is committed in a Firestore transaction. A crash can leave an unreferenced object; it cannot publish a reference to an object whose upload did not finish. No automatic deletion policy is applied to analysis history.

All modifying API requests include `request_id` and `expected_revision`; create requires revision 0. Reusing the same key with identical input returns the same operation without dispatching another Job. Reusing it for different input or editing a stale revision returns 409. Inputs freeze once extraction starts. Corrections to region, date, original maps or metadata require a new analysis.

One operation can own a run at a time. Different analyses can execute concurrently. A queued dispatch has a 15-minute reservation. A worker atomically claims its unique operation, renews a 120-second lease every 30 seconds, and checks operation ID, owner and unexpired lease before every manifest update. Duplicate worker starts fail before inference. Expired reservations become interrupted when read; late workers cannot publish results or overwrite a successor's failure state. An ambiguous Job launch is recorded and is never automatically redispatched.

Each validated stage publishes a checkpoint. Selective resume reconstructs state from the checkpoint immediately before the requested phase and executes that phase and its successors. Only phases whose prerequisites exist can be selected. Evidence-version checks reject stale resumes; report resumes additionally require the exact confirmed evidence hash. Explicit resumes may repeat a remote call whose outcome was uncertain. Neither Job dispatch nor model inference is claimed to execute exactly once.

Manifests have a conservative 800 KB transaction limit and 500 modifying request limit; large bodies live in GCS. An analysis reaching this audit limit requires a new run. Shared access follows the app's existing authenticated deployment boundary; the service does not implement a new individual-user access model. Keep the app/API behind that boundary, since users can read each other's runs and generate signed download URLs by design.

## API surface

All paths below are relative to `/seasonal-outlook`. Errors use 400 for input/contract errors, 404 for missing resources, 409 for stale/concurrent modifications and 503 for disabled/unconfigured processing.

| Method | Path | Behavior |
|---|---|---|
| GET | `/info` | Configuration status, model, regions, categories and limits; no inference |
| POST | `/runs` | `request_id`, `expected_revision: 0`, `region_id`, ISO `report_date`, optional `notes` |
| GET | `/runs` | Shared history; optional `region_id`, `report_date`, `status`, `limit`, `before` timestamp |
| GET | `/runs/{id}` | Manifest, versions, operations and availability of exports |
| POST | `/runs/{id}/maps` | Multipart: one `file`, request ID, expected revision, `product`, optional `issue_date`, `note` |
| GET | `/runs/{id}/input` | Validated input and calendar/checklist limitations |
| GET | `/runs/{id}/maps/{index}` | Original image; zero-based index |
| POST | `/runs/{id}/extract` | Reserve the three evidence phases |
| POST | `/runs/{id}/feedback` | Current `version_id` and literal `comments` |
| POST | `/runs/{id}/confirm` | Current `version_id`, `confirmed: true`; reserve the report phases |
| POST | `/runs/{id}/resume` | Prior `operation_id`, `stage`, optional timeout |
| GET | `/runs/{id}/versions[/{version}]` | Version inventory or complete evidence |
| GET | `/runs/{id}/attempts[/{operation}]` | Attempt inventory or checkpoint state and diagnostics |
| GET | `/runs/{id}/input-package` | Signed URL for frozen original input ZIP, available after extraction starts |
| GET | `/runs/{id}/download-link/{name}` | Signed export URL, 10-minute lifetime; optional prior `operation_id` |
| GET | `/runs/{id}/artifacts/{name}` | Small export bytes; exports over 20 MB require a signed URL |

Input bounds: static PNG/JPEG/WebP, 1–12 maps, 30,000,000 bytes per image, 50,000,000 bytes combined, 45,000,000 pixels per image. Duplicate bytes are rejected. The UI uploads individual maps, rather than one oversized multipart package.

The three tabs show evidence beside the original map, version comparisons, comments/decisions, reports/downloads and inputs/checklists. Region starts unselected. URLs carry `seasonal_run`. Polling is confined to an active-operation fragment; waiting for analyst review leaves no polling loop. Rendering, tab changes and refreshes never invoke a model. Explicit confirmation is required in the UI and enforced independently by the backend.

Exports include Word with/without map appendix and an artifact ZIP with rules, input, original images, evidence versions, review decisions, requests, responses and checkpoints. Original maps are preserved byte-for-byte in ZIP/GCS. Word appendix rasters are bounded to 300 dpi at page size (1,860 × 1,800 pixels); WebP is converted to PNG only in that appendix to ensure compatibility and bound memory. Prior reports remain available under their own operation after new feedback invalidates the current report.

## Company deployment

The user will configure the company project manually in the browser console. Follow [the console setup guide](../deploy/seasonal-outlook/CONSOLE_SETUP.md) for that deployment. It includes the exact Job, IAM, index and environment settings; Terraform below is an optional equivalent reference, not a required step. Do not apply Terraform to manually created resources without importing them first.

Use `deploy/seasonal-outlook/main.tf` with explicit company parameters. This module is additive: it creates the Seasonal bucket, worker identity, Job, IAM grants and history indexes; it references the existing Firestore Native database and app service account. It does not recreate the app, database, authentication or other workflows. Import pre-existing Seasonal resources into the Terraform state before applying, if necessary. Store Terraform state under the company's normal infrastructure controls.

1. Build the same app image for the web service and worker. Pin `image` to its digest. The existing Dockerfile includes the new code and bundled resources. Run the offline tests and the Linux container check below.
2. Copy `terraform.tfvars.example` to a private values file. Supply company project, Job region, separate Seasonal bucket name, worker account ID, existing app account email, existing database, Job name and image digest. No personal gcloud defaults are consulted by application code.
3. Authenticate deployment tooling using the company identity and project. Run `terraform init`, `terraform validate`, `terraform plan -out=seasonal.plan`; review the additive changes, then apply the approved plan. No Terraform commands have been applied as part of the local implementation.
4. Wait for the Job, IAM grants and Firestore indexes to be ready. Add `terraform output -json app_environment` entries to the existing app service with the normal deployment pipeline, preserving all unrelated environment settings. The requested active deployment uses `SEASONAL_DRAFTER_ENABLED=true`; replace an existing explicit `false` if present. A source-code default cannot override an existing environment variable.
5. The Job has one task, 2 vCPU, 4 GiB RAM, 7,200-second timeout and zero task retries. Its command is `python`; the app supplies `-m app.services.seasonal_outlook.worker --run ... --operation ...` via execution overrides. Ordinary manual execution with no overrides displays help only. The web image still starts Streamlit normally.
6. Run the acceptance checklist below against the configured company deployment. Application and Terraform defaults now enable processing, as requested; use an explicit `false` for a staging hold or rollback. Setting the feature flag false blocks new operations while preserving history and downloads; already reserved Jobs may finish.

IAM grants: worker `aiplatform.user`, both identities `datastore.user` and Seasonal-bucket `storage.objectUser`; the app gets a custom two-permission `run.jobs.run`/`run.jobs.runWithOverrides` role **on this Job**, plus `iam.serviceAccounts.signBlob` **on the signing worker identity**. Firestore and Vertex predefined roles are project grants; confirm these fit company policy. No service-account key files are required. The deployer separately needs authority to build/pull the image, create these resources/roles, bind IAM and act as the worker identity.

The SDK explicitly sends `X-Vertex-AI-LLM-Request-Type: shared` for online PayGo; it does not request Priority/Flex tiers or provision dedicated serving/GPUs. Availability, quotas, Preview approval, regional policy and billing must be checked in the company project. [Cloud Run job execution and overrides](https://docs.cloud.google.com/run/docs/execute/jobs) require more than plain invocation when passing per-run arguments.

Local verification commands:

```powershell
.\venv\Scripts\python.exe -m pytest tests\test_seasonal_outlook.py -q
.\venv\Scripts\python.exe -m pytest tests -q
```

Linux/container verification when a Docker daemon is available:

```sh
docker build -t vam-llm-seasonal:test .
docker run --rm --entrypoint python vam-llm-seasonal:test -m app.services.seasonal_outlook.worker --help
docker run --rm --entrypoint sh vam-llm-seasonal:test -c 'pip install pytest httpx && python -m pytest tests/test_seasonal_outlook.py -q'
```

## GCP and analyst release gate

Live GCP acceptance is deliberately separate from offline synthetic fixtures. In the company staging deployment:

- Verify image digest parity, enabled APIs, database/index readiness, private bucket access, signing permission, Job overrides and Vertex access to the exact Preview model. Confirm no API-key or Together dependency.
- Upload real AFY, AMX and ASE input packages manually. Check the region remains explicit and AFY does not acquire a Horn of Africa section. Validate issue dates, modes and product classifications with the analyst.
- Complete extraction, visual review and refinement. Compare evidence with original maps; record actual model, prompt hashes, latency and token counts. Historical outputs are comparison material, not ground truth.
- Submit at least one analyst feedback cycle, inspect applied/unverifiable decisions, compare versions and explicitly confirm the latest version. Validate report citations, geography, observed/mixed/forecast language and omissions scientifically.
- Repeat an action with the same request ID and stale revision; verify one reserved operation. Have two browsers modify the same revision; verify a 409 for the losing change. Dispatch the same worker twice; verify only one reaches inference. Run two different analyses concurrently.
- Close the browser while a Job runs, reopen the shared URL from another user/instance, and verify progress and artifacts persist. Cancel a Job after a checkpoint, wait for lease expiry, then resume a selected phase. Verify completed predecessor phases do not call the model again and the old worker cannot commit.
- Validate empty/truncated/blocked responses using controlled test doubles or a staging test harness, not invented scientific examples sent to production. Verify errors retain saved responses/checkpoints and require explicit resume.
- Open Word with/without maps and ZIP exports, including a large package retrieved through a signed GCS URL. Verify an expired URL is rejected and a new one can be requested. Disable processing and confirm shared history/downloads remain available.
- Record analyst acceptance and GCP observations for the active deployment. Activation alone is not evidence that these checks passed. Prototype local-run import and automatic map retrieval remain out of scope.

## Local verification record

Rechecked on 21 September 2026 after changing the activation default: **23 Seasonal tests passed** (30.45 seconds), including enabled-by-default behavior, explicit disablement and prevention of new runs without company configuration. No cloud calls were made.

Verified on 18 September 2026, without company GCP credentials or live model inference:

- Full existing application suite with the initial Seasonal tests: **1,265 passed, 4 skipped, 4 expected failures**, 883.53 seconds.
- Final Seasonal suite: 22 tests, including AFY/AMX/ASE synthetic flows, feedback, exact-version confirmation, stale actions, duplicate dispatch, expired-worker fencing, selective resume, invalid responses, input limits, API/dispatcher parity and Streamlit refresh behavior. The two final SDK tests exercise real Google Gen AI serialization through an in-process HTTP test transport, including all six phases and zero retries on HTTP 503.
- New Python modules compile, and `git diff --check` passes. Terraform HCL parses successfully (13 resource declarations); provider-level `terraform validate/plan` is still a company deployment check.
- Word exports with/without maps were opened and rendered by Microsoft Word; rendered pages were visually inspected. ZIP contents and image appendix presence were checked programmatically. LibreOffice is unavailable on this workstation, so the Word rendering was used for visual QA. The fixtures are synthetic, not scientific validation.
- Actual Streamlit page checked in the browser with processing disabled; corrected local contrast and wrapped the existing assistance banner without changing shared styling.
- Linux container execution was not run: Docker CLI exists but its daemon is not running. Docker commands above remain a release prerequisite.
- No Terraform apply, company GCP run, quota check, model-quality acceptance or production enablement was performed. These require the explicit company deployment parameters and analyst acceptance described above.
