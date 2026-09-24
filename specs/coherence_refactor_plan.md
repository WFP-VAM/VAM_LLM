# Coherence refactor plan (GCP): LangGraph for Seasonal, no checkpoint layer, dead-code removal

**Status: PLAN APPROVED IN DESIGN; implementation not started.** Prepared 2026-09-24 from a read-only inspection of `VAM-LLM-Sep2026` @ `eb667ab` (local clone `vam-llm-app`). Decisions D1–D4 were taken on 2026-09-24 (section 2); the plan below reflects them.

---

## 0. Scope and constraints

Three changes:

1. Seasonal Outlook runs on a LangGraph graph, like Market Monitor (MM) and MFI.
2. No drafter has a checkpoint/recovery layer. Seasonal keeps only what its human review step needs.
3. Dead code is removed.

Constraints:

- The result must deploy and run on the **current GCP setup** (Cloud Run service, Vertex AI, Firestore/GCS, Cloud SQL), with **no new cloud resources**. The AWS migration comes later; this plan only avoids making it harder.
- The Streamlit UI keeps working. When a feature is removed (e.g. MFI Resume), its page is updated in the same step.
- Model calls, prompts, analytical logic and report content are unchanged. This is a structural refactor.

Starting point (verified in code):

- No drafter uses a LangGraph checkpointer. MM (`market_monitor/graph.py:3470`) and MFI (`mfi_drafter/light_graph.py:223`) call `graph.compile()` without one.
- MFI's checkpoints are a custom journal (`mfi_drafter/execution.py`). Seasonal's are custom too (`seasonal_outlook/service.py`, `worker.py`), and its stages run as a plain list (`engine.CHAINS`), not a graph.
- MM has no recovery: a failed run is rerun. That is the target for all three.

---

## 1. Design answers

### 1.1 Does Seasonal need something across the human review? Yes: its analysis record, not a checkpoint layer

- A **checkpoint layer** saves *execution state* so that an interrupted run can continue where it stopped: stage checkpoints, cursors, leases, resume.
- An **analysis record** saves the *work product* the analyst reviews and approves. Seasonal needs one because:
  - the review pause lasts minutes to days, across Cloud Run instances and restarts;
  - during the pause the analyst looks at maps and evidence versions, compares versions and writes comments;
  - confirmation binds the report to one exact evidence version (hash), for audit;
  - analyses are shared and reopened by URL.

After the pause, the next phase needs only domain data: the frozen maps, the evidence version to revise or the confirmed one, the analyst's comments. It never needs the execution state of the previous phase. So Seasonal keeps its Firestore/GCS analysis record and loses its checkpoint layer.

| Kept: analysis record (Firestore + GCS) | Removed: checkpoint/recovery layer |
|---|---|
| Inputs: region, date, notes, frozen maps | Per-stage checkpoints and cursor (`worker.py:53-68`) |
| Evidence versions (immutable) and `current_evidence` | Selective resume from a stage (`service.py:207-224`) |
| Analyst comments; confirmation (version + hash) | Worker claim, 120 s lease, 30 s heartbeat, fenced writes (`service.py:268-292`, `worker.py:12-25`) \* |
| Per-operation record: kind, status, timestamps, error, the phase's final output, exports | 15-minute queued reservation, dispatch ownership, "ambiguous dispatch" handling (`service.py:232-266`) \* |
| Request/response objects of each model call (they feed the audit ZIP) | Checkpoints inside the audit ZIP (`exports.py:85-86`) |
| `request_id` / `expected_revision` on every action (double clicks, two analysts acting at once) | |
| One active operation per analysis, with a deadline after which a stuck operation shows as interrupted and can be retried | |

\* With the Cloud Run Job (not chosen, D1) a one-time claim would have stayed; running in the web service, the "one active operation" rule in the manifest transaction is enough.

MM and MFI keep only their **run record** (`app/shared/async_runs.py`: status, progress, result, artifacts), which is not a checkpoint layer either.

### 1.2 How the pause works with LangGraph: phase graphs, not `interrupt()`

LangGraph's native human-in-the-loop (`interrupt()` and resume) needs a **checkpointer**: the paused graph state must be stored somewhere durable. That would be a checkpoint layer, which change 2 rules out. It would also duplicate the analysis record and tie the API to LangGraph's internal state format.

Instead, Seasonal gets **one graph with three entry points**, one per phase. Each invocation runs its phase from start to end:

```
             ┌─ extract ──► extraction ─► review ─► refinement ─────────────► END   → awaiting_review
START ─phase─┼─ feedback ─► feedback ───────────────────────────────────────► END   → awaiting_review
             └─ report ───► draft ─► report_review ─► redraft ─► export ─────► END   → completed
```

The analyst pause is the gap *between* two invocations. This is how the original prototype worked ("No automatic edge crosses the human checkpoint", `MVP_ver2/ui_streamlit/agent/graph.py`), and how the app already behaves. The stage logic is reused unchanged: `engine.request_for`, `engine.accept`, the science contracts, prompts and provider. The graph replaces the stage loop in `worker.py`.

### 1.3 Target pattern

| | Market Monitor | MFI | Seasonal |
|---|---|---|---|
| Workflow | LangGraph, one graph | LangGraph, one graph | LangGraph, one graph with 3 entry points |
| Checkpointer / recovery layer | none | none (journal removed) | none (checkpoints and resume removed) |
| Runs in | background thread of the web service | background thread of the web service | background thread of the web service (D1) |
| Persists | run record | run record | analysis record (needed by the review pause) |
| On failure | run again | run again (Resume removed) | retry the failed phase from its inputs |
| In-run robustness (unchanged) | QA correction loop | 2 attempts per call, repair of invalid sections, truncation handling, request splitting (kept, in memory) | response saved before validation |

### 1.4 The Seasonal "leftovers" are not LangGraph remnants

The prototype's LangGraph code (`agent/graph.py`, `UIState`) was never ported. What remains comes from the prototype's **research harness** (experiment arms `control`/`rules`, claim evaluation) and from its **old checkpoint-recovery helpers**:

| Symbol | Origin | Used today |
|---|---|---|
| `science/state.py` `WorkflowState` (`input_path`, `arm: control\|rules`, `run_dir`, …) | research runner state | only by `RefinementState`, itself unused |
| `science/schemas.py` `Claim`, `Paragraph`, `Priority`, `RegionalAnalysis`, `ClaimAssessment`, `ReferenceAssessment`, `Evaluation`, `audit_links` | research evaluation contracts | no |
| `science/refinement_schemas.py` `RefinedExtraction`, `RefinementState`, `validate_refinement` | older refinement contract | no |
| `science/refinement_schemas.py` `VisualReview`, `MapReview`, `validate_review` and the check classes | older review contract | only by `validate_saved_review` (unused) |
| `science/evidence_contract.py` `validate_saved_review`, `recover_prefix_only` | prototype checkpoint recovery | no |
| `science/refinement_schemas.py` `evidence_diff`, `METADATA_FIELDS` | | **yes** (page 5), kept |
| `science/schemas.py` `Record`, `Signal`, `MapReading`, `EvidenceBundle`, `StudyRule` | | **yes**, kept |

Proposal (D2): keep `state.py` as a file and rewrite it as the new graph state (`SeasonalState`); delete the unused research and recovery symbols.

---

## 2. Decisions

### Decided (2026-09-24)

| # | Decision | Chosen | Not chosen | Affects |
|---|---|---|---|---|
| D1 | Where Seasonal phases run on GCP | **Background thread of the web service, like MM and MFI.** `jobs.py`, `worker.py`, the Cloud Run Job and its dispatcher role go away; `SEASONAL_JOB` and `SEASONAL_JOB_REGION` are no longer required. Needs the Cloud Run settings MM/MFI already rely on (CPU always allocated) and enough memory for Word/ZIP exports (the Job has 4 GiB). If an instance shuts down mid-phase, that phase fails and the analyst retries it. | **Keep the Cloud Run Job.** The Job runs the same phase graph; a one-time claim stays; infrastructure unchanged. More robust to instance shutdowns, but Seasonal stays the only drafter that runs differently. | Phase 4, deploy docs |
| D2 | Seasonal leftovers (§1.4) | Rewrite `state.py` as the graph state; delete the unused research/recovery symbols | Keep them untouched | Phases 4–5 |
| D3 | MFI parallel scheduling | **Native LangGraph edges**, as in MM and Seasonal: remove the futures/thread-pool wrapper (`light_graph.py:94-238`) and run `charts` alongside the drafts. Cost: each correction waits for the slower of the two reviews (estimated ≤ 1–2 min on a ~10 min run). Without resume, the wrapper's other benefit ("successful branches are kept when another fails") no longer applies. | Keep the futures scheduler, minus the journal | Phase 2 |
| D4 | Old results after deploy | **Not needed**: remove the readers for historical claim-based MFI results and for old Seasonal manifests | Keep read compatibility for existing MFI results and Seasonal analyses | Phases 3–4 |

### Defaults (applied unless you object)

| # | Default |
|---|---|
| D5 | The MFI JSON endpoints `/generate` and `/generate-async` build a report on synthetic data **only with an explicit `use_mock_data=true`**, as MM does. Today they silently fall back to mock data when no CSV is given (`light_graph.py:30-31`). |
| D6 | Seasonal "Retry failed phase" keeps today's 600 / 1200 / 1800 s timeout choice. |
| D7 | Seasonal evidence versions are published at the end of their phase (extraction still publishes V1 and V2). A failed phase publishes only its diagnostics. |
| D8 | `reliable_contracts.py` keeps its name (it holds live contracts); renaming is left to the AWS migration. `synthetic_fixtures.py` stays in the package (live tests and the acceptance script use it). |
| D9 | Work on branch `refactor/gcp-coherence` from `VAM-LLM-Sep2026`; tag the starting point; one commit per step; push and PR only after review. |

---

## 3. Work plan

Every phase ends with the full test suite (~17 min) and a Streamlit smoke test of every page (`streamlit.testing.v1.AppTest`). MFI goes first (Phases 1–3, the largest cleanup), then Seasonal (Phase 4), small cleanups (Phase 5), docs (Phase 6) and verification (Phase 7). Phases 4 and 5 do not depend on Phases 1–3.

### Phase 0 — Safety net

1. Tag `pre-coherence-20260924` on `VAM-LLM-Sep2026` (`eb667ab`) and create the branch.
2. Baseline: `python -m pytest tests -q` and `python scripts/check_mfi_reliable.py`; record the counts in this document.
3. Baseline MFI outputs on the local benchmark CSVs (Benin, Haiti, Gaza in `MFI Test Databases/`) with the fake model client the tests use: deterministic analysis, tables and figures, saved for comparison after Phases 1–3.

**Baseline recorded 2026-09-24** on `refactor/gcp-coherence` @ `ac2bdb0` (same code as `eb667ab`), with the repository venv (Python 3.12.4, langgraph 1.0.5):

- Full suite: **1,303 tests: 1,294 passed, 9 skipped, 0 failed** (8 min 25 s). Skipped: 2 live DataBridges smoke tests (no live credentials), 5 `test_mfi_r0_artifact` tests (no generated report on disk), 2 Postgres tests (`TEST_POSTGRES_DATABASE_URL` unset).
- MFI gate subset (`test_mfi*` plus the 6 shared files of `scripts/check_mfi_reliable.py`), counted from the same run: 973 tests, 5 skipped.
- MFI output snapshot: Benin, Haiti and Gaza run through the live workflow with a fake model and fake retrievers (7 calls each; 159/162/161 report blocks; 18/20/20 figures rendered for real), plus mock data, context retrieval and the `/dimensions` endpoint. It captures analysis, exact model prompts and schemas, report blocks, DOCX text and figure hashes. Two runs are identical.
- Tooling and outputs are in `.tmp/coherence-baseline/` (git-ignored): `snapshot_mfi.py`, `compare_snapshots.py`, `mfi-A/`, `junit-full.xml`, `baseline-per-file.txt`.

### Phase 1 — MFI: move live code out of dead modules (no behaviour change)

| From | To | Used by |
|---|---|---|
| `graph.node_context_retrieval` (`graph.py:1622`, ~100 lines) | new `mfi_drafter/context.py`, as a copy without the old workflow's diagnostics bookkeeping (same documents, traces and context status). `graph.py` keeps its own version until Phase 3. | `light_graph.retrieve_context` |
| `graph.generate_mock_mfi_data` (`graph.py:1285`) | new `mfi_drafter/mock_data.py` | `light_graph.prepare_analysis` (D5) |
| `DIMENSION_DESCRIPTIONS` imported via `graph` | import from `methodology.py:87`, where it is defined | `router.py:923`, `dispatcher.py:62` |
| `simple_orchestration.build_market_prompt_projection` | `light_evidence.py` | `light_evidence.py:75` |
| `response_contracts.provider_schema` | `light_contracts.py` | `light_contracts.py:44` |
| `narrative.legacy_narrative_aliases` (`narrative.py:2547`) | `compatibility.py` for now; dropped in Phase 3 if only historical results need it (D4) | `compatibility.py:12` |

The old modules re-export what moved (`graph.py` from `mock_data.py`, `simple_orchestration.py` and `response_contracts.py` from their new homes), so the old workflow and its tests keep working until Phase 3 deletes them. New tests cover `context.py` and `mock_data.py` directly, so the live path keeps its coverage when Phase 3 removes the old tests. (`test_seerist_retrieval`, `test_llm_runtime_config` and `test_mfi_analysis` reach *old-workflow* functions through `graph`; they are handled in Phase 3.)

**Verify:** full suite green; MFI outputs identical to the Phase 0 baseline.

**Done 2026-09-24.**
- Full suite: 1,311 tests, 1,302 passed, the same 9 skipped, 0 failed. The only difference from the baseline is the 8 new tests (`test_mfi_context.py`, `test_mfi_mock_data.py`).
- MFI output snapshot identical to the baseline on all three benchmarks and on the misc checks.
- Every Streamlit page renders without exceptions. Locally, page 5 shows its usual "Seasonal durable storage is not configured" message, because `SEASONAL_PROJECT` and `SEASONAL_BUCKET` are not set.
- The light workflow no longer imports `graph.py`. `router.py` and `dispatcher.py` still import its failure-reconciliation helpers, which Phase 3 removes.

### Phase 2 — MFI: remove the checkpoint layer

1. `light_runtime.py`: `ModelRuntime` keeps its per-run bookkeeping in an in-memory, thread-safe ledger instead of the journal.
   - **Kept:** token budget and counting (cached in memory), 2 attempts per work item, repair of only the invalid sections, truncation handling, request splitting (`Oversized`), per-call diagnostics.
   - **Removed:** replay of captured responses across executions, epochs, stored object references.
   - `RecoveryError` is replaced by a small `MFIRunError(status_code)` in `errors.py`. The "attempts exhausted" message tells the user to run the report again.
2. `light_graph.py`: nodes call their functions directly (no `execute_once`); figures render directly (`light_graph.py:63-73`); phase status and progress come from the ledger. D3 is applied here.
3. `light_service.py`:
   - `prepare_submission` keeps only the basemap preflight, renamed `validate_submission`;
   - `schedule_resume`, `execute_resumed` and `get_light_run` are removed;
   - `run_mfi_report_generation` returns the result with `llm_diagnostics` and `generation_diagnostics` taken from the ledger;
   - `effective_contract()` stays, as metadata in the result and in `/info`.
4. Delete `execution.py`, `execution_service.py`, `drafts.py`.
5. Router and dispatcher (both, since Streamlit goes through the dispatcher):
   - remove `POST /resume/{id}`, `GET /draft/{id}`, `GET /analysis/{id}` and `POST /export-draft-docx/{id}` (`router.py:688-735`, `dispatcher.py:588-611`);
   - `/status` and `/result` read the shared run record, like MM (`router.py:58-59` and `:658-686`, `dispatcher.py:1081-1137`). This also fixes the shadowed `get_run` import in the router;
   - `MFIReportStatusOutput` loses the recovery fields.
6. `pages/4_MFI_Drafter.py`: the recovery panel (`:147-194`) becomes a progress panel showing the phase table and errors. Resume, the analysis download and the incomplete-draft download are removed.
7. Tests:
   - rewrite `test_mfi_light_runtime` and `test_mfi_light_workflow` without the resume, ownership and recovery cases;
   - adapt `test_mfi_map` and `test_mfi_drafter_ui`;
   - delete `test_mfi_storage_compatibility` and `test_mfi_reliable_api`.
8. Scripts: `mfi_light_acceptance.py` loses `--resume-failed`; `check_mfi_reliable.py` gets the new test list.

**Verify:** full suite; MFI outputs identical to the baseline; AppTest on page 4.

### Phase 3 — MFI: delete the old workflows and the offline tooling

1. Delete about 15k lines of modules: `graph.py`, `narrative.py`, `simple_orchestration.py`, `qa_pipeline.py`, `review.py`, `correction.py`, `reliable_nodes.py`, `packages.py`, `response_runtime.py`, `response_contracts.py`, `deterministic_report.py`, `release_validation.py`, `report_inspector.py`, `r0_diagnostic.py`, `offline_narrative_fixtures.py`.
2. Router and dispatcher failure handlers: remove the branches that reconcile old-workflow diagnostics (`reconcile_*`, `graph.py:547/688/846`). Check whether `MFIGenerationBlockedError` can still be raised; remove its handling if not.
3. `mfi_drafter/__init__.py`: rewrite the lazy exports (today it lists `run_mfi_report_generation` twice).
4. Remove the readers for historical claim-based results (D4) in `app/shared/report_blocks.py`, `app/shared/docx_export.py` and `compatibility.py`. The exact list is established in this step; the light report path (`_apply_mfi_layout_contract`, persisted `report_blocks`) is kept.
5. Tests:
   - **Delete** (they only cover deleted code): `test_mfi_fail_closed_pipeline`, `_phase3`, `_r0_artifact`, `_r0_diagnostic`, `_r0_inspector`, `_r0_synthetic`, `_r3_delivery_policy`, `_r4_qa_traceability`, `_reliable_execution`, `_reliable_recovery_graph`, `_reliable_delivery`, `_response_contracts`, `_response_resume`, `_simple_orchestration`, `_typed_propagation`, `_unverified_figure_delivery`, `_claim_identity_delivery`.
   - **Split** (keep the tests of live modules, drop the rest): `test_mfi_analysis`, `_benchmark_regression`, `_phase4`, `_r2_aggregation_metadata`, `_r3_narrative_safety`, `_r5_visualizations`, `_r7_context_and_evidence`, `_r8_layout_density`, `_reliable_inputs`.

**Verify:** `git grep` finds no import of a deleted module; `python -c "import main"`; full suite; MFI outputs identical to the baseline; AppTest on every page.

### Phase 4 — Seasonal: LangGraph phase graph, no checkpoint layer

1. `science/state.py` becomes `SeasonalState` (D2): `phase`, `pack`, `images`, rules and profiles, `evidence_v1`, `review`, `evidence`, `issue_resolutions`, `allocations`, `analyst_comments`, `feedback_resolutions`, `initial_analysis`, `draft_review`, `report`.
2. New `graph.py`:
   - one `StateGraph(SeasonalState)` with a conditional entry on `phase` (§1.2);
   - one node per stage, built from `engine.request_for` → `provider.complete` → `engine.accept`, plus an `export` node (`exports.build`);
   - compiled **without a checkpointer**;
   - each node records its request, response and call diagnostics through a small recorder (for the audit ZIP and for progress). No state checkpoints.
3. New `runner.py`, replacing the loop in `worker.py`:
   - builds the phase input from the analysis record (maps; current evidence + comments; confirmed evidence);
   - streams the graph and marks completed stages;
   - at the end, publishes the evidence versions (D7), stores the phase output and the exports, and sets the status;
   - every final write checks the operation is still the active one, so a late thread cannot overwrite a newer operation.
4. `service.py`:
   - remove `claim`, `owned`, `heartbeat`, lease renewal, dispatch ownership and the "uncertain dispatch" handling;
   - `get()` marks an operation interrupted once its deadline (stages × timeout + margin) passes, instead of a lease;
   - `resume` becomes `retry`: the whole failed phase from the same inputs (D6);
   - phases are launched according to D1;
   - operations are listed by `created_at`. This fixes the page picking the "last" operation by Firestore map order (`pages/5_…py:149, 221`).
5. `commands.py`: `Resume` becomes `Retry(operation_id, timeout)`. `api.py`: `/attempts[/{op}]` becomes `/operations[/{op}]`, returning the operation and its phase output; `POST …/retry` replaces `…/resume`.
6. `exports.py`: the audit ZIP contains each operation's output instead of its checkpoints.
7. Delete `jobs.py` and `worker.py` (D1). The service starts `runner.py` in a background thread of the web process, as the dispatcher does for MM and MFI. `config.py` stops requiring `SEASONAL_JOB` and `SEASONAL_JOB_REGION`.
8. `pages/5_Seasonal_Outlook_Drafter.py` and `ui.py`:
   - progress comes from completed stages;
   - the report tab reads the operation output (today it reads the last checkpoint, `:218-235`);
   - the "Attempts, review decisions and selective resume" panel (`:237-259`) becomes "Operations and review decisions", with a "Retry failed phase" button.
9. No compatibility with old analysis manifests (D4).
10. Tests (`test_seasonal_outlook.py`):
    - **Keep:** full workflow and exports, idempotency conflicts, confirmation invalidation, feature flag, configuration, input limits, unknown IDs, FastAPI/dispatcher parity, Gemini configuration and GCS images, SDK wire conversion, no SDK retries, bounded Word appendix, "UI refresh never starts inference".
    - **Replace** the resume, fencing and checkpoint tests with: phase retry, deadline expiry, a late thread cannot overwrite, report retry requires the confirmed evidence hash.
    - **Delete** (D1): duplicate and ambiguous Job dispatch.
    - `test_seasonal_upload_ui` is unchanged.

**Verify:** Seasonal tests; full suite; AppTest on page 5 (no inference on render).

### Phase 5 — Remaining dead code

| Area | Remove |
|---|---|
| Market Monitor | `/dataset/status` and `/dataset/upload` 404 stubs (router `:916-930`, dispatcher `:2004-2017`); the `load_csv_price_data` and `_upload_file_to_gcs` shims and the unused GCS imports (`data_loader.py:57-59`, `:142-150`); unused `_report_status` (router `:78`) |
| Dispatcher | `_save_temp_file`, `_get_food_basket_commodities` |
| Shared | `app/shared/gcs.py`; the `DataBridgesAuth` and `DataBridgesClient` classes in `app/shared/databridges.py` (its constants move next to `price_cache/databridges_adapter.py`) and `tests/test_databridges_client.py`; the `MFI_MARKET_DRAFT_TIMEOUT_SECONDS` / `MFI_RED_TEAM_TIMEOUT_SECONDS` settings in `app/shared/llm.py`, used only by the deleted MFI workflow |
| Seasonal | the research/recovery symbols of §1.4 (D2) |
| Infrastructure | `supervisord.conf`, `nginx.conf.template` |
| Dependencies | `langchain-google-genai`, `chardet`, `openpyxl` (no imports; re-checked before removal) |

**Verify:** full suite; `pip install -r requirements.txt` in a clean venv plus `pip check`.

### Phase 6 — Docs and configuration

- `README.md`, `docs/app-overview.md`: the three drafters as LangGraph graphs without checkpoints; Seasonal's phase graphs and analysis record.
- Update `specs/mfi_light_workflow.md` (remove the recovery sections), `specs/seasonal_outlook_implementation.md` (execution, persistence and concurrency sections) and `deploy/seasonal-outlook/` (drop the Job, the worker identity's Job permissions and the `seasonalJobDispatcher` role; keep bucket, Firestore indexes and signer).
- Add a "Historical — superseded 2026-09" header to `specs/mfi_reliable_workflow.md`, `mfi_drafter_fail_closed_qa.md`, `mfi_drafter_r0_regression_tooling.md`, `mfi_drafter_r2_semantics.md`, `mfi_drafter_r3_narrative_safety.md` and `mfi_drafter_phase4_*`.
- `.env.example`: drop `SEASONAL_JOB` and `SEASONAL_JOB_REGION` and the two MFI timeout settings; add `MFI_DRAFTER_ANALYSIS_VERSION=2`, which MFI needs and which is missing today.

### Phase 7 — Verification and release

- **Automated:** full suite with the expected count changes; no reference to deleted modules; `import main`; dispatcher and FastAPI smoke tests; AppTest on every page.
- **On GCP:** deploy the branch as a **Cloud Run revision with no traffic**, then run:
  - one MM bulletin;
  - one MFI report (Benin CSV);
  - one full Seasonal cycle (extract → feedback → confirm → report → downloads);
  - one Seasonal retry after a forced failure.
- **Rollback:** route traffic back to the previous revision. Seasonal analyses created by the new version will not display in the old one, because the operation format changes.

---

## 4. Impact on the GCP deployment

| Item | Change |
|---|---|
| New cloud resources | none |
| Environment variables | `SEASONAL_JOB` and `SEASONAL_JOB_REGION` no longer used. `MFI_MARKET_DRAFT_TIMEOUT_SECONDS` / `MFI_RED_TEAM_TIMEOUT_SECONDS` ignored if set. Nothing else. |
| Cloud Run service | CPU always allocated (MM/MFI already need it); memory sized for Seasonal exports (compare with the Job's 4 GiB); consider min instances ≥ 1 |
| IAM | The web service identity already has Vertex and the Seasonal Firestore/GCS permissions (the Terraform grants both identities); the signer setup is unchanged. The Job and the `seasonalJobDispatcher` role can be removed later. |
| Firestore / GCS | unchanged: same collection, bucket and indexes |
| MFI `mfi/checkpoint` subdocuments and `mfi-recovery` GCS prefix | no longer written (they exist only if the durable run backend was configured) |

## 5. User-visible changes

- **MFI:** no Resume, and no download of the analysis or the incomplete draft of a failed run. A failed report is run again.
- **Seasonal:** "Resume from phase" becomes "Retry failed phase" (the whole phase); the audit ZIP no longer contains checkpoints.
- **MFI JSON API** without a CSV requires `use_mock_data=true` (D5).
- Everything else is unchanged: inputs, analyses, reports, Word files and exports.

## 6. Risks

| Risk | Mitigation |
|---|---|
| MFI output changes by mistake | Phase 0 baseline compared after Phases 1–3: analysis, tables and figures must be identical |
| A deleted module is still imported somewhere (e.g. lazily) | Import checks, `git grep`, full suite and AppTest after every phase |
| Seasonal thread killed by an instance shutdown (D1) | Deadline marks it interrupted; the analyst retries the phase; min instances ≥ 1 |
| Real-model behaviour cannot be tested locally | Fake-client tests locally; real calls on the no-traffic revision (Phase 7) |
| Rollback after new Seasonal analyses exist | Previous revision stays available; see Phase 7 |

## 7. Out of scope

AWS migration items (Bedrock, S3, Step Functions); unifying LLM observability across the three drafters; a shared background-runner abstraction; authentication; the MFI release gate; the `figure_id` that page 5 rebuilds; security hardening (`.dockerignore`, tracebacks in API responses); renaming `reliable_contracts.py`.
