# MFI reliable workflow

> **Historical — superseded 2026-09.** This document describes the claim-based MFI workflow, which the September 2026 coherence refactor removed (`coherence_refactor_plan.md`, Phase 3). It is kept as a dated record; the current workflow is described in `mfi_light_workflow.md`.

This revision implements the coordinated `mfi-reliable-v1` workflow for newly loaded processed CSVs. Keep `MFI_DRAFTER_ANALYSIS_VERSION=2`. Methodology remains `databridge-current`; analytical and narrative schema versions are 2.1. Existing 2.0 reports remain readable through the existing compatibility path. No SDK or model migration is included.

## Response contract bundle v2

New runs use `mfi-reliable-contracts-v2`. The 15-node LangGraph coordinator and its conditional correction stages remain. Context, dimension, market, executive, review and typed-patch transport schemas now share one registry. Schemas, their hashes and validated examples supply the prompt instructions and each invocation's JSON binding. The installed Vertex SDK conversion is checked explicitly; each invocation gets a detached schema because the SDK can mutate its input dictionary. No cached model client is reconfigured.

The MFI invocation boundary checkpoints extracted responses before parsing and structural validation. Raw responses and staged candidates use the existing run-access/storage model and are excluded from public metadata. This does not enable global prompt capture. Invalid fields have identified paths and issues; valid claims remain staged, rather than disappearing from lists.

Initial drafting and review permit one structural repair per failed target per execution epoch, in compatible groups of at most four. An unparseable response gets one syntax repair, which consumes the same allowance and must preserve its content tokens. Fields outside authorized paths cannot change; target IDs and expected field hashes are checked. Saved replies can be processed after interruption without another model call. Typed correction retains its own existing allowance without an additional nested repair loop. Transport retries, model and timeout settings are unchanged. Storage, ownership and configuration errors do not invoke format repair.

Context statements are validated independently, including citation arrays and exact supplied source passages. An unresolved optional classification is disclosed as degraded, retains accepted statements and can allow generation to continue. A valid empty classification or retrieval with no results is complete. Manual Resume on an eligible failed run retries unresolved classification and keeps accepted statement identities. Changed accepted context or disclosure invalidates dependent executive/review inputs; independent analytical outputs, charts and dimension/market drafts are reused. Previously corrected context is kept distinct from the classification dependency fingerprint.

Progress derives from current journal work items; model attempts remain separate from logical work. Superseded candidate reviews remain inspectable without becoming pending current work. Status adds `response_contract_bundle`, `structural_validation_issues`, `structural_repair_summary`, `degraded_work_count`, `context_classification_outcome` and `unresolved_context_statement_count`. Incomplete drafts identify partial dimensions and include accepted fragments plus unresolved-field findings. Required structural failures block final publication, including when the numerical-disclosure exception would otherwise apply.

Version-1 checkpoints remain inspectable but cannot resume under bundle v2. Completed historical reports remain readable and exportable. This bundle requires no backend migration, new cloud resources or additional environment variables.

## Changes

- Stable source identities, aliases, duplicate detection, source-row/value lineage, explicit date parsing, count/coordinate validation, and separate parsing/applicability states.
- One analytical ledger and application-rendered facts, including threshold counts and ties; complete dimension evidence appears in the main report or analytical annex.
- Nine independent dimension drafts, market batches of at most five, semantic review batches of at most 40 claims, and homogeneous correction batches of at most four targets. Actual serialized outgoing messages and schemas are checked against 160,000 characters.
- Typed correction contracts, independent target validation, exact context-text normalization, one additional JSON/schema repair attempt per failed target, staged responses and field-hash checks.
- Immutable, checksummed input/artifact objects, deduplicated snapshot field references, transactional execution ownership and fenced commits. A heartbeat renews the 120-second lease every 30 seconds.
- Separate incomplete snapshot/export paths, manual Resume, browser run links, and spawned serial rendering workers. Each figure has a 120-second timeout and one retry.

Rendering launches a dedicated Python module, so it cannot re-execute Streamlit's page script when starting a worker. Correction batches are partitioned using the actual serialized messages and bound response schema, with the same complete regional mean/denominator comparisons used during drafting.

Stored scores, formula tolerances, unweighted means, the exact 15-market selection rule, Haiti's six MFIr exclusions, recommendation restrictions and the existing final unverified-number delivery policy remain in force.

## Storage and deployment compatibility

MFI uses the application's existing async-run backend selection on both local hosts and Cloud Run. Existing memory deployments need no new environment variables or services. The original reliable-workflow release introduced a cloud-only durable-storage requirement; that restriction has been removed to preserve the previous deployment configuration. No deployment resource, billing or shared-service backend behavior is changed.

With no run-storage configuration, or with `RUNS_BACKEND=memory`, checkpoints use memory. Status reports `recovery_storage=process_local` and a `recovery_limitation` displayed beside the run in Streamlit. Completed work and incomplete drafts can be reused while the same server process remains available; recovery after a process restart or on another instance is unavailable. A run URL identifies the run but does not make its storage persistent. An in-process model failure can still be resumed manually without repeating committed work.

Already configured Firestore/GCS deployments continue using their existing `RUNS_BACKEND`, `RUNS_GCS_URI`, `RUNS_FIRESTORE_DATABASE` and `RUNS_FIRESTORE_COLLECTION` settings. Backend aliases, URI autodetection and configuration normalization remain those of the shared async-run service. The execution identity needs read/write access to the existing run collection and GCS prefix. Checkpoints are stored under each run's `mfi/checkpoint` subdocument; immutable objects use the `mfi-recovery` prefix. Once the durable backend is selected, failed durable operations block progress rather than diverting that run to memory. Full-prompt capture is not enabled.

Background execution remains subject to the existing Cloud Run CPU and instance lifecycle settings. Google's [background activity guidance](https://docs.cloud.google.com/run/docs/tips/general#background_activity) describes CPU availability outside requests; this compatibility fix does not change billing or provision resources. Restart recovery requires an existing durable backend. Status polling never schedules work.

A run records the contract bundle, methodology, schemas, claim identity version, model/runtime configuration and dependency versions. Resume rejects incompatible bundles/configurations and changed inputs. Rollback affects new submissions; preserved checkpoints remain non-resumable where the recorded bundle is unsupported. Historical failures without checkpoints, including `mfi_3523f014`, cannot be reconstructed from diagnostic logs.

## Interfaces

Existing generation/status/result/artifact/final-DOCX interfaces remain. New routes:

- `POST /mfi-drafter/resume/{run_id}` with `expected_revision` and `idempotency_key`: 202 scheduled/replayed; 409 conflict, active/completed or non-resumable run; 404 unknown run; 503 unavailable recovery storage.
- `GET /mfi-drafter/draft/{run_id}?snapshot_revision=...`: an immutable incomplete narrative snapshot and QA findings.
- `POST /mfi-drafter/export-draft-docx/{run_id}` with optional `snapshot_revision`: a `DRAFT-` DOCX with the incomplete label in its title and page headers. Export does not publish, change QA or unlock final export.
- `GET /mfi-drafter/analysis/{run_id}`: validated analytical data as soon as analysis is saved.

Status adds workflow/run revisions, execution state, resumability/reason, recovery storage and its limitations, draft revision, analytical availability and evaluated QA counts. The original pending/running/completed/failed status vocabulary is preserved. Both API and in-process Streamlit use the same execution journal, recovery and export service.

## Verification and release gate

Run `python scripts/check_mfi_reliable.py` in the project environment. It selects the complete MFI suite plus affected shared-service tests on either Windows or Linux. The supplied source CSVs remain local/untracked in `MFI Test Databases`; tests that require them skip if absent. The committed baseline contains analytical regression expectations.

Validated local dependencies: Python 3.12.4; pandas 2.3.3; numpy 2.4.0; pydantic 2.13.4; langgraph 1.0.5; langchain-core 1.4.0; langchain-google-vertexai 3.2.3; matplotlib 3.10.8; python-docx 1.2.0; google-cloud-firestore 2.27.0; google-cloud-storage 3.10.1. Dependencies are recorded per run rather than silently changing a resumed run's contract.

Windows verification on 8 September 2026: the full MFI/shared sweep returned 840 passes, four existing expected failures and two historical-schema assertion failures. Those two expectations were corrected to preserve 2.0 inputs and passed in the subsequent 53-test compatibility/API/correction rerun. Additional focused runs passed 28 full-data input/package/recovery tests, 37 API/storage/shared-interface tests and 14 identity/applicability tests. The full-data checks cover both supplied CSVs, all nine dimension and correction packages, complete review comparator tables, unchanged analytical baselines, and the audited Benin counts. A Streamlit-followed-by-rendering run also verified worker isolation. No live model run was performed.

Before promotion, run the same tests inside the Python 3.11 Linux container, installing the project's test runner if necessary. The implementation host's Docker Linux engine was unavailable, so Linux execution has not been certified locally.

Storage compatibility verification on 9 September 2026: 87 targeted API, Streamlit-dispatch, recovery, delivery and shared-service tests passed, including Benin CSV submission under the previous Cloud Run memory configuration. Both Benin/Haiti analytical baseline checks also passed. The cloud-only 503 was reproduced before the fix. These checks use local execution and mocked model/cloud boundaries; no live generation or infrastructure change was performed.

The owner will run live validation after the commit/push:

Response-bundle verification on 9 September 2026: the expanded Windows MFI/shared-service sweep passed 914 tests with four existing expected failures. Subsequent focused runs passed 92 recovery/storage/observability tests and 80 contract/context/orchestration tests after the final changes. These include real response validators with malformed model stubs, the exact Availability polarity and context citation-array failures, preservation of valid fragments, capture-before-validation interruption recovery, partial-draft disclosures, context-dependent regeneration, immutable provider schemas, missing/extra market rows, and renamed-country numerical invariance. Both complete supplied CSV baselines passed. Docker's engine was unavailable; live Vertex calls and Linux container execution remain deployment validation gates.

1. Submit Benin and Haiti in a test deployment. Confirm 53 and 68 Full-MFI markets respectively, with six Haiti exclusions. Check unchanged priority ordering and scores against the baseline.
2. Inspect every dimension's main-body/annex coverage. Verify Service at/below median = 37/53, Infrastructure below 3 = 7, and Oueme Food Quality mean = 4.79. Inspect the DOCX and all chart labels.
3. Inject a generation failure after completed narrative batches; download the marked draft and Resume once in the same server process. Verify saved batches/figures and call IDs are reused. Where a durable backend is already configured, additionally stop the server, wait for lease expiry and verify recovery on a new process; that restart check does not apply to memory deployments.
4. Repeat an identical Resume idempotency key and send a stale revision. Confirm no duplicate execution and the documented conflict response. Confirm final endpoints remain locked for incomplete snapshots.
5. Confirm the reported storage capability, outgoing package sizes, retries, model latency, QA findings and rendering memory; verify durable writes only where durable storage is already configured. Keep this as a test deployment until these live and Linux checks pass.
6. Exercise a failed context classification followed by a later required-stage failure. On Resume, confirm only unresolved context fields retry, accepted statement IDs remain stable, independent drafts/charts are reused, and changed context refreshes the executive summary and review packages. A valid empty classification must not be retried.
