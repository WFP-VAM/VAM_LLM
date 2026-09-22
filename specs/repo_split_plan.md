# Repo split plan: VAM LLM (UNIFIED APP) / MarketAIssist

**Status: DRAFT FOR APPROVAL — nothing has been modified yet (no files, no git).**
Prepared 2026-09-22 from a read-only inspection run at 10:18–10:20 CEST (`_split_tmp/phase1_status.log`) and a test baseline run started 10:20 CEST (`_split_tmp/phase1_tests.log`). Line numbers refer to the working tree at commit `c3b35d3` (UNIFIED APP) and `48c9fd3` (MarketAIssist).

Conventions used below: **R1** = `vam-llm` archive repo, **R2** = `UNIFIED APP` (the VAM LLM app), **R3** = `MarketAIssist`.

---

## 1. Verified state of the three repositories

### 1.1 Summary table

| # | Path | HEAD / branch | Remotes | Working tree |
|---|---|---|---|---|
| R1 | `C:\Users\eugen\VAM LLM Materials\vam-llm\` | `DEV` @ `0e9dd33c` (2026-02-12) — **ahead of `origin/DEV` by 1** | `origin` → `https://github.com/WFP-VAM/vam-llm.git` | **Dirty**: 1 modified + 2 deleted notebooks, many untracked dirs (see 1.2) |
| R2 | `...\vam-llm\VAM LLM - Prototypes\UNIFIED APP\` | `phase_3_faster_MFI` @ `c3b35d3` (2026-09-21) — up to date with `origin/phase_3_faster_MFI` | `origin` → `https://github.com/edacrema/VAM-UNIFIED-APP.git` (PUBLIC), `wfp` → `https://github.com/WFP-VAM/VAM_LLM.git` (PUBLIC) | Clean except 6 untracked paths (see 1.3) |
| R3 | `...\vam-llm\VAM LLM - Prototypes\MarketAIssist\` | `main` @ `48c9fd3` (2026-07-28) — up to date with `origin/main` | `origin` → `https://github.com/edacrema/MarketAIssist.git` (PRIVATE) | Clean except 2 untracked data dirs (`outputs/`, `test datasets/`) |

Common facts: no stashes in any repo; no tags in any repo; R2 and R3 each have exactly one worktree (their own); R1 has a second worktree `C:/Users/eugen/.claude-worktrees/MARKET MONITOR GENERATOR/flamboyant-noether` on branch `flamboyant-noether` (not touched by this plan). Both R2 and R3 carry `refs/codex/turn-diffs/*` (Codex CLI checkpoints): they are not branches and are never pushed by the explicit `git push <remote> <branch>` commands used in this plan (only `--all`/`--mirror` would push them — both are banned here). Git 2.43.0.windows.1, gh 2.88.1, both venvs are Python 3.12.4 (`C:\Python312`).

### 1.2 R1 — `vam-llm` (archive)

```
$ git status
On branch DEV
Your branch is ahead of 'origin/DEV' by 1 commit.
Changes not staged for commit:
        modified:   VAM LLM - Prototypes/Active Warnings Updates Agent/Notebook_GEMINI.ipynb
        deleted:    VAM LLM - Prototypes/MARKET MONITOR GENERATOR/Notebook_1.ipynb
        deleted:    VAM LLM - Prototypes/MFI Troubleshooter/Notebook.ipynb
Untracked files: (23 entries, among them)
        VAM LLM - Prototypes/MarketAIssist/
        VAM LLM - Prototypes/UNIFIED APP/
        VAM LLM - Prototypes/UNIFIED APP UI/
        ... (Active Warnings Updater/Updates Agent, HH DATA NORMALIZER, HH_Report_NORMALIZER,
             MFI Report Generator, MFI/PRICE Troubleshooter, ver2 - market bulletin - tests, _split_tmp/)

$ git branch -vv
* DEV                  0e9dd33c [origin/DEV: ahead 1] Add hotspot_agent app files for HH Report Normalizer
  HH_Report_NORMALIZER 0e9dd33c Add hotspot_agent app files for HH Report Normalizer
+ flamboyant-noether   60afc9bf (C:/Users/eugen/.claude-worktrees/MARKET MONITOR GENERATOR/flamboyant-noether) MFI Troubleshooter Nonotebook ver1
  main                 795d9574 [origin/main] closing evaluation pipeline

$ git remote -v
origin  https://github.com/WFP-VAM/vam-llm.git (fetch/push)      # ls-remote: DEV=60afc9bf, evaluation_pipeline=795d9574, main=795d9574

$ git log --oneline -5
0e9dd33c Add hotspot_agent app files for HH Report Normalizer
60afc9bf MFI Troubleshooter Nonotebook ver1
2225e114 Notebook Market Monitor Generator ver1
f9376e96 Upload Active Warning Updater Notebook
795d9574 closing evaluation pipeline
```

Flags for R1: (a) `git check-ignore "VAM LLM - Prototypes"` → not ignored, so R2 and R3 show up as untracked directories of R1 — this is the nesting to dissolve; (b) the unstaged notebook modifications/deletions and the 1 unpushed commit on `DEV` are **pre-existing and outside the scope of this plan**; they are not touched, but Eugenio should know they exist; (c) `_split_tmp/` (this plan's scratch folder) is untracked in R1 and will be deleted at the end.

### 1.3 R2 — `UNIFIED APP` (VAM LLM app)

```
$ git status
On branch phase_3_faster_MFI
Your branch is up to date with 'origin/phase_3_faster_MFI'.
Untracked files:
        MFI Manuals/                                   (7 files, 8.6 MB — reference PDFs/HTML)
        MFI_Drafter_2_0_workflow.html
        MFI_analysis_language_style_discussion_draft.md
        evals/mfi_analysis_style_prompt_review.md
        specs/aws_step_functions_assessment.md         (NOTE: the AWS assessment cited in the brief is NOT committed)
        workflow_slides/                               (contains .build/node_modules — 7,778 untracked files)

$ git branch -vv
  docker-streamlit      4230077 [origin/docker-streamlit] vertex AI compatibility implemented
  docker-streamlit-only 83d16c3 [origin/docker-streamlit-only] Migrate Databridges connector to WFP gateway
  local-streamlit       b072b70 [origin/local-streamlit] streamlit UI improvement
  main                  83d16c3 [origin/main] Migrate Databridges connector to WFP gateway
  phase_3               48e5c6e [origin/phase_3] Make MFI response validation and recovery country independent
  phase_3_complex       1430cb5 fix(mfi-drafter): shard Red-Team coherence review
* phase_3_faster_MFI    c3b35d3 [origin/phase_3_faster_MFI] Restore multi-map Seasonal upload with optional metadata
  phase_3_ver1          524cc9a feat(price-validator): fetch per-country market list from DataBridges
  prov_1                bdd3027 Implement country food baskets

$ git remote -v
origin  https://github.com/edacrema/VAM-UNIFIED-APP.git   # ls-remote heads: docker-streamlit, docker-streamlit-only, local-streamlit, main, phase_3, phase_3_faster_MFI (all == local)
wfp     https://github.com/WFP-VAM/VAM_LLM.git            # ls-remote heads: main=02dd3c9 (2026-08-26), phase_3=bd869fd (2026-08-27) — matches local remote-tracking refs, nothing new upstream

$ git log --oneline -5
c3b35d3 Restore multi-map Seasonal upload with optional metadata
d9d0b31 Integrate Seasonal Outlook Drafter with Vertex and Cloud Run Jobs
073e588 Refine MFI recommendations, analytical style and executive summary
caa66b4 Add offline MFI basemap and collision-free numbered market labels
4322fd4 Simplify MFI drafting with parallel review and drafter corrections
```

Branch topology (measured with `git rev-list --left-right --count`):

| Local branch | vs `phase_3_faster_MFI` | vs personal `origin` | vs `wfp` | Verdict |
|---|---|---|---|---|
| `phase_3_faster_MFI` | — | in sync | no branch on wfp | **live working branch** |
| `phase_3` | 5 behind, 0 ahead | in sync | `wfp/phase_3` is 3 behind local (fast-forwardable) | live (integration line) |
| `main` | 61 behind, 0 ahead | in sync | **`wfp/main` is 49 commits AHEAD of local `main`** (wfp/main = `02dd3c9`, 2026-08-26, confirmed ancestor of `phase_3_faster_MFI`; local main = `83d16c3`, June 2026, confirmed ancestor of `wfp/main`) | local `main` is stale; never push it to wfp |
| `phase_3_complex` | 16 behind, 0 ahead | not on any remote | — | merged, dead |
| `phase_3_ver1` | 32 behind, 0 ahead | not on any remote | — | merged, dead (this is the commit the MarketAIssist validators derive from — see 3.4) |
| `prov_1` | 53 behind, 0 ahead | not on any remote | — | merged, dead |
| `docker-streamlit-only` | 61 behind, 0 ahead | in sync | — | merged, dead (== main) |
| `docker-streamlit` | 78 behind, 0 ahead | in sync | — | merged, dead |
| `local-streamlit` | 82 behind, **2 ahead** | in sync | — | only branch NOT merged: 2 commits from Jan 2026 (`894c210` "Add Streamlit UI for local runs", `b072b70` "streamlit UI improvement"), superseded by later UI work |

Other R2 observations: `.gitignore` contains `docs/` yet `docs/app-overview.md` and `docs/second_food_basket.md` are tracked (added before the rule) — editing them works normally, but any **new** file under `docs/` would need `git add -f`. `.claude/settings.local.json` is tracked (permission allow-list only, no paths). `.tmp/` is gitignored but weighs **3.4 GB / 31,416 files**, with ~55 sub-directories returning "access denied" to the current user (sandboxed pytest temp dirs). `app/price_data.csv` (151 MB) and `app/docker.env` are gitignored. Data dirs: `MFI Test Databases/` = 3 files / 13.9 MB, ignored as required. Line endings: `core.autocrlf=true` is in effect in both repos (system-level Git for Windows default; `git ls-files --eol` shows `i/lf` for every checked file), so **the index is LF everywhere and git normalises on commit**; only the working trees differ (R2 `main.py` w/crlf, `streamlit_shared.py` and `databridges.py` w/mixed, `llm.py` w/lf; R3 `main.py`/`streamlit_shared.py` w/lf, `llm.py` w/crlf, `databridges.py` w/mixed). Consequence: copying a file from one working tree to the other produces a clean, content-only diff in git; raw byte comparisons outside git (as used in Phase 1) must use `--strip-trailing-cr`.

### 1.4 R3 — `MarketAIssist`

```
$ git status
On branch main
Your branch is up to date with 'origin/main'.
Untracked files:
        outputs/          (15 files, 1.2 MB — generated reports)
        test datasets/    (16 files, 11.5 MB — real datasets, NOT ignored by .gitignore)

$ git branch -vv
* main                            48c9fd3 [origin/main] feat(price-validator): harden country commodity validation
  marketassist-ver1-with-template af225bb [origin/marketassist-ver1-with-template] feat(price-validator): fetch per-country market list from DataBridges

$ git remote -v
origin  https://github.com/edacrema/MarketAIssist.git   # ls-remote == local for both branches

$ git log --oneline -5
48c9fd3 feat(price-validator): harden country commodity validation
8076d8d feat(price-validator): use country DataBridges reference lists
50d609c refactor(price-validator): remove template dependency
af225bb feat(price-validator): fetch per-country market list from DataBridges
4033c6f feat: replace bug-report link with second-testing-phase notice
```

`marketassist-ver1-with-template` (`af225bb`) is an ancestor of `main` (merged). Root commit: `d810efd` "feat: initial MarketAIssist app split from unified VAM LLM prototype" (2026-07-24 10:31). **`test datasets/` and `outputs/` are untracked but not ignored**: a careless `git add .` would commit real data — see step 4 (add them to `.gitignore`).

### 1.5 GitHub permissions (gh, read-only)

- `gh auth status`: logged in as `edacrema`, token scopes `gist, read:org, repo`.
- Org `WFP-VAM`: membership `active`, role `member`; org settings: `members_can_create_repositories = true` (private and public). **Creating `WFP-VAM/<new repo>` with `gh repo create` is possible with the current token.**
- `viewerPermission = ADMIN` on all four repos: `WFP-VAM/VAM_LLM` (public, default `main`, pushed 2026-08-27), `WFP-VAM/vam-llm` (private), `edacrema/VAM-UNIFIED-APP` (public), `edacrema/MarketAIssist` (private). No open PRs anywhere.
- Verified with the follow-up run (`_split_tmp/phase1b_gh.log`, 10:39 CEST): the org has **59 repositories and none is a MarketAIssist repo** (closest names: `MarketFunctionalityIndex`, `market-price-forecasts`, `GlobalMarketMonitor`, `IranMarketMonitor` — all unrelated projects), so the org repo has to be **created**. `WFP-VAM/VAM_LLM` has exactly two branches (`main`, `phase_3`) and **`main` is not branch-protected** (HTTP 404 on the protection endpoint). Ancestry confirmed with `git merge-base --is-ancestor`: `wfp/main` ⊂ `phase_3_faster_MFI`, `wfp/phase_3` ⊂ `phase_3_faster_MFI`, local `main` ⊂ `wfp/main` — i.e. everything planned for WFP-VAM is a fast-forward or a new branch. The personal account also holds two older, unrelated repos (`edacrema/VAM-LLM`, `edacrema/VAM-LLM-UNIFIEDAPP-UI`, Jan 2026) that this plan does not touch.

---

## 2. Inventory of validator references in R2 (verified with `git grep --untracked` on the full tree, then line-by-line on the files)

### 2.1 Remove entirely

| Path | Notes |
|---|---|
| `app/services/mfi_validator/` — `__init__.py`, `graph.py`, `router.py`, `schemas.py` | byte-identical to R3 |
| `app/services/price_validator/` — `__init__.py`, `graph.py`, `router.py`, `schemas.py` | R3 is ahead (see section 3) |
| `pages/1_MFI_Validator.py`, `pages/2_Price_Validator.py` | Streamlit pages |
| `tests/test_price_validator_market_names.py` | 7 tests (the only validator test file) |
| `app/shared/assets/mfidata_validator.jpeg`, `app/shared/assets/pricedata_validator.jpeg` | referenced only by `pages/1_How_To_Use_The_Tools.py:131,171`; `wfp_logo.png` and the two drafter JPEGs stay |

### 2.2 Modify

| File | Exact places | Change |
|---|---|---|
| `main.py` | imports L6–7; `include_router` L47–48; services list L58–59; also title/description L21–22 (still says "validazione dati") | drop the two routers and two service entries; description → report generation only |
| `streamlit_app.py` | L109–133: the two-column landing menu contains `MFI Dataset Validator` (L110–116) and `Price Data Validator` (L127–133) | remove the two buttons; re-flow the menu to three drafters (Price Bulletin, MFI Report, Seasonal Outlook) |
| `app/streamlit_backend/dispatcher.py` | import L48 (`RAW_FILE_INDICATORS, run_mfi_troubleshooting`) and L71 (`run_price_troubleshooting`); function block **L581–L1133** (`_dispatch_mfi_validator` … `_price_validator_info`, i.e. everything up to `_dispatch_mfi_drafter` at L1134); services list L2831–2832; routing L2856–2873 (`if service == "mfi-validator"` / `"price-validator"`) | delete; helpers `_save_temp_file`, `_extract_file`, `_get_form_value` are also used by drafter code and **stay** |
| `pages/1_How_To_Use_The_Tools.py` | L66 (Validators bullet), L78–79 (TOC), L103–L181 (sections "1. MFI Dataset Validator" and "2. Price Data Validator", incl. `_render_tool_image` at L131/L171); headers "3." and "4." at L183/L223 to renumber | rewrite the intro as three drafting tools; add a short "Need to validate a dataset first?" pointer to MarketAIssist; renumber; **note: the page has no Seasonal Outlook section today** (decision D7) |
| `pages/0_Tester_Onboarding.py` | L38 ("Whenever you would normally validate a dataset or draft a report…"), L48 (bug-report example is an MFI *validator* case) | reword to drafting; replace the example with a drafter case |
| `docs/app-overview.md` | whole document: mission (two projects), "four services", MarketAIssist tables L19–24, architecture diagram L50, layout L152/154/167/168, "Validation (MarketAIssist)" L179–186 | rewrite for VAM LLM only (three services incl. Seasonal Outlook, which is currently missing from the doc), one paragraph pointing to MarketAIssist |
| `.env.example` | L36 `MARKET_NAMES_GCS_URI=` | remove (only reader: `app/shared/gcs.py:43`, only caller: `price_validator/graph.py`); **keep all `WFP_V2_*` / `DATA_BRIDGES_*`** (used by `app/shared/databridges.py`, `price_cache/databridges_adapter.py` and their tests) |
| `app/services/market_monitor/data_loader.py` | L154 — deprecated shim message "Use the Price Data Validator to validate raw files before DataBridges upload." | reword to "Use MarketAIssist (Price Data Validator) …" — text only |
| `app/shared/gcs.py` (optional) | `get_market_names_gcs_uri()` L42–44 and `get_market_names_cache_path()` L47–51 become dead after removal; docstring L1 says "shared by validation tools" | optional cleanup commit; `parse_gcs_uri` / `download_gcs_to_file` / `get_gcs_client` are used by `market_monitor/data_loader.py` and stay |
| `requirements.txt` (optional) | `chardet` (only importer: `mfi_validator/graph.py`) and `openpyxl` (only consumer: Price Validator `read_excel`) | optional removal — no other `read_excel`/`chardet` usage found in `app/`, `pages/`, `scripts/`; default in this plan: **leave** (deployment image unchanged), revisit later |
| `README.md` | **does not exist in R2** | create (definition of done requires it) |
| `evals/eval_framework_overview.md` | none of the eval docs are rewritten | add a 3-line introductory note (allowed by the brief) |

### 2.3 Hidden couplings — explicit answers

- **`RAW_FILE_INDICATORS`**: defined in `app/services/mfi_validator/graph.py:60`. Used in `mfi_validator/graph.py` (L405–437), `mfi_validator/router.py` (L17, 330, 339), `mfi_validator/schemas.py:178` (docstring) and `dispatcher.py` L48 (import), L777, L786 (inside `_mfi_validator_info`, part of the block being deleted). **No usage in `app/services/mfi_drafter/`** (the MFI drafter has its own `input_validation.py`, and a full-tree `git grep RAW_FILE_INDICATORS` returns only the files above). → **Deleted with the validator; nothing to relocate.**
- **`WFP_PRODUCTS`**: defined in `price_validator/graph.py:47`; used in `price_validator/graph.py` (L407, 661), `price_validator/__init__.py` (re-export), `price_validator/router.py` (L358–364, `/products`) and `dispatcher.py` L877–882 (local import inside `_dispatch_price_validator`). **No drafter usage.** → **Deleted with the validator.**
- **`app/shared/countries.py` (`supported_country_options()`)**: used by both apps; byte-identical; stays in both.
- **`app/shared/databridges.py`**: in R2 the runtime consumers are `price_validator/graph.py:31` (`get_databridges_client`, removed with the validator) and `price_cache/databridges_adapter.py:16` (imports only the `DEFAULT_*` constants; token exchange goes through the external `data_bridges_client.WfpApiToken`, not through `DataBridgesAuth`). After the split the `DataBridgesAuth/DataBridgesClient` classes in R2 are exercised only by `tests/test_databridges_client.py`.
- **`app/shared/gcs.py`**: `MARKET_NAMES_GCS_URI` is validator-only (see 2.2). `market_monitor/data_loader.py:57` imports `download_gcs_to_file` and `parse_gcs_uri` (generic).
- **`streamlit_shared.py`**: no validator mention at all (`grep -i validat` → nothing). Validator pages import only generic helpers (`run_async_and_poll`, `render_results_tabs`, …) that drafter pages use too.
- **Residual routes/imports**: `git grep -E "mfi_validator|price_validator|mfi-validator|price-validator" -- . ':!specs' ':!evals'` returns exactly the items listed in 2.1/2.2 plus `.env.example:36`. No script under `scripts/` references the validators. No test asserts on the length or content of the `/` services list, so shrinking it to three entries breaks nothing.
- **Beware of false positives** in the acceptance grep: the word "validator" also appears legitimately in drafter code and tests (MFI claim validator: `tests/test_mfi_phase3.py::test_validator_*`, `release_validation.py`, `input_validation.py`, pydantic `field_validator`). The acceptance criterion is the four identifiers above, not the word "validator".

### 2.4 Historical mentions — left untouched (as instructed)

`specs/phase_1/mission.md`, `specs/phase_2/roadmap.md`, `specs/phase_2/vision.md`, `specs/phase_3/fable_audit_report-cache_system.html`, and the six committed `evals/*.md` (2–8 mentions each). The untracked `specs/aws_step_functions_assessment.md` and `evals/mfi_analysis_style_prompt_review.md` are not part of the repo (decision D9).

### 2.5 Reverse check — R3 references to the UNIFIED APP

Only cosmetic: `README.md:5–6` (already states the split), `app/shared/databridges.py:171` User-Agent `UNIFIED_APP/DatabridgesConnector`, `app/shared/gcs.py:50` cache dir `~/.cache/unified-app`, `app/shared/async_runs.py:153` Firestore db name `vam-llm-async`, and `tests/test_dispatcher_endpoints.py:37–38` which **asserts** that `/market-monitor/info` and `/mfi-drafter/info` return 404 (good: it proves independence). No code dependency on R2. The Firestore database name and User-Agent are runtime identifiers shared with the deployed environment and are **not** renamed by this plan.

---

## 3. Shared modules: diff results and file-by-file decision

Comparison done on the working trees (both clean vs HEAD), byte-level and EOL-insensitive.

| File | Result | Decision |
|---|---|---|
| `app/shared/__init__.py`, `async_runs.py`, `gcs.py`, `countries.py` | **byte-identical** | do not touch |
| `app/shared/databridges.py` | R3 ahead: `+import uuid`; strips credentials; `_looks_like_secret_reference()` (`:latest`, `projects/`, `/secrets/`, `sm://`) with actionable messages; `uuid.UUID(api_key)` check. 33 changed lines, nothing else differs | **port R3 → R2** (step 6) **together with the test adaptation**: R2's `tests/test_databridges_client.py` builds `DataBridgesAuth("client-id", …)` / `DataBridgesClient("key", …)` / `WFP_V2_API_KEY=new-key` (L68–72, 101, 126, 148–156, 174, 192, 236, 253, 265, 296–302, 332) — all non-UUID, so **~8 tests would fail** without changing the fixtures to a UUID constant (R3 already does this: `CLIENT_ID = "00000000-0000-4000-8000-000000000001"`, plus 2 negative tests). Price Bulletin / price cache is unaffected (it does not use `DataBridgesAuth`). |
| `app/shared/llm.py` | R2 ahead: `LLMRuntimeConfigurationError` (code `llm_runtime_configuration_invalid`), `LLMRuntimeConfig`, `LLMRuntimeStatus`, validated `LLM_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` / `LLM_MAX_OUTPUT_TOKENS` / `MFI_*_TIMEOUT_SECONDS`, per-setting model cache, `get_model(timeout_seconds=, max_retries=)`. R3 = old singleton (timeout 60, retries 2, lenient `LLM_MAX_OUTPUT_TOKENS`) | **port R2 → R3 verbatim** (step 7). Compatibility checked: R3 calls `get_model()` with no arguments only (`price_validator/graph.py:448,486,592`, `mfi_validator/graph.py:1015`); `configure_model` unused in R3; `pydantic` already installed. **Behaviour with no env vars set**: same model (`gemini-2.5-pro`), same location (`us-central1`), same retries (2), **default timeout changes 60 s → 90 s**; `LLM_MAX_OUTPUT_TOKENS` set to a non-integer now raises instead of being ignored; the two `MFI_*_TIMEOUT_SECONDS` settings are drafter-only and only validated if someone sets them. R3 `.env.example` gains `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` (optional, commented). A trimmed `tests/test_llm_runtime_config.py` (defaults + invalid values, without the `mfi_drafter` imports of the R2 version) is added to R3. |
| `streamlit_shared.py` | 25 top-level symbols in common, 23 identical. Only in R2: `_llm_elapsed_seconds`, `render_llm_diagnostics`, `render_mfi_raw_table_downloads`, `render_report_blocks`, imports from `app.services.mfi_drafter.table_projection` and `app.shared.report_blocks`. Different in both: `render_run_status` (R2 adds MFI LLM/generation diagnostics panels) and `run_async_and_poll` (R2 adds an optional `on_started` callback used by drafter pages). Also `INSTRUCTIONS_PAGE_URL`: R2 hard-codes a Cloud Run URL (`https://vam-llm-marketaissist-…run.app/How_To_Use_The_Tools`), R3 uses the relative `/How_To_Use_The_Tools`. | **The difference is drafter-only. Nothing to port to R3.** (No timeout/polling fix exists in R2 that R3 lacks; `request_json` and the timeout tests are identical.) Not touched in either repo. |
| MFI Validator (4 files + `pages/1_MFI_Validator.py`) | byte-identical | nothing to port; R3 keeps its copy, R2 deletes its copy |
| Price Validator (4 files + `pages/2_Price_Validator.py` + test) | R3 ahead, as the brief says: `layer2_template_comparison` removed, graph `layer1 → layer3 → report`, `_fetch_commodity_names_from_databridges` (`/Commodities/List`), `_suggest_markets` (LLM market-name suggestions with per-country cache), richer error detail. R2's copy is frozen at commit `524cc9a` (last commit touching the validator dirs in R2). | nothing to port R2 → R3; R2 deletes |
| `main.py`, `streamlit_app.py`, `dispatcher.py`, `Dockerfile`, `requirements.txt`, `.gitignore`, `.env.example`, `cloudbuild.yaml` | diverge as expected (different service sets / images) | not aligned — they are app-specific |

**Confirmation of the "more advanced version" claims**: verified in both directions; no case was found where the version indicated in the brief is not the most advanced one.

### 3.4 History pointer (for the READMEs, no history rewrite)

- R3 root commit `d810efd` (2026-07-24) contains `price_validator/graph.py` and `schemas.py` blobs that appear in R2 history exactly at **`524cc9a`** ("feat(price-validator): fetch per-country market list from DataBridges", 2026-07-24, branch `phase_3_ver1`, an ancestor of `phase_3_faster_MFI`); the MFI validator blobs date from `508c0bc` (2026-02-16) and were never changed afterwards. So: **"validators derived from UNIFIED APP commit `524cc9a0646f77c5b483d5c56cac4e146ecc3617`"** (MFI validator last changed in `508c0bc6748360af1dbdf4584117587ac8dbaea7`). R3 root commit: `d810efdc35a992ac7880fe0a77d575addde4207a`; R3 current: `48c9fd3edd05f9a572236eff527e299e746fd169`; R2 current: `c3b35d3bd030f1836e06a26ae53999e2c27e7ada`. Symmetrically the R2 README will point to `https://github.com/WFP-VAM/MarketAIssist` (or whatever D2 decides) and to the R3 root commit `d810efd` / current `48c9fd3`.

---

## 4. Test baseline (before touching anything)

Run 10:20–10:55 CEST with each repo's own `venv\Scripts\python.exe` (3.12.4), `-p no:cacheprovider`, no `.env` read or printed by the harness (the app's own `load_dotenv()` still runs, as it does for Eugenio's normal runs). `git status --porcelain` was re-checked after the runs: unchanged in both repos (only `specs/repo_split_plan.md` — this document — appeared as untracked in R2). `import main` succeeds in both repos; the R2 FastAPI app currently exposes route prefixes `/market-monitor` (27 routes), `/mfi-drafter` (17), `/seasonal-outlook` (1), `/mfi-validator` (6), `/price-validator` (7) plus `/`, `/health`, docs — after Phase 2 the last two prefixes must be gone.

| Repo | Command | Collected | Result | Expected after Phase 2 |
|---|---|---|---|---|
| R2 UNIFIED APP | `python -m pytest tests -q` | **1293 tests** (collection 43.8 s) | **1285 passed, 4 skipped, 4 xfailed** in 17 min 27 s (exit 0) | **−7** (`tests/test_price_validator_market_names.py`), **+2** new dispatcher/FastAPI tests (validator routes → 404, services list == 3) → 1288 |
| R2 UNIFIED APP | `python scripts/check_mfi_reliable.py` | (all `tests/test_mfi*.py` + 6 shared test files) | **969 passed, 4 xfailed** in 14 min 55 s (exit 0) | unchanged (the gate does not include validator tests) |
| R3 MarketAIssist | `python -m pytest tests -q` | **55 tests** (collection 33.2 s) | **54 passed, 1 skipped** in 13.6 s (exit 0) | **+4–6** (`tests/test_llm_runtime_config.py`, trimmed); `test_databridges_client.py` unchanged → ~59–61 |

Tests expected to disappear in R2 (all 7 in one file): `test_market_names_fetched_from_databridges_for_country`, `test_market_names_accepts_iso3_code_directly`, `test_market_names_cached_per_country`, `test_market_names_fall_back_to_gcs_when_databridges_fails`, `test_market_names_error_mentions_both_failed_sources`, `test_empty_market_list_is_an_error`, `test_country_flows_through_public_entrypoints`. Their R3 counterparts already exist and are richer (`test_price_validator_market_names.py`, `_market_suggestions.py`, `_commodity_names.py`, `_template_removal.py`).

Tests that must **not** be removed although they contain "validator": `tests/test_mfi_phase3.py::test_validator_*` (12), `test_mfi_r2_aggregation_metadata.py`, `test_mfi_r3_narrative_safety.py`, `test_mfi_simple_orchestration.py::test_reduced_validator_*` — they test the MFI drafter's claim validator.

---

## 5. Operation sequence (Phase 2, only after approval)

Legend per step: **Repo** · **Commands** (PowerShell, run from the repo root unless stated) · **Expected effect** · **Verify** · **Undo**. Commit messages are conventional-commit style, in English. No `--force`, no rebase of pushed branches, no remote branch deletion, no `push --all/--mirror`. Every push is an explicit `<remote> <branch|tag>`.

### Step 0 — Preflight (read-only)
- **Repo**: all. **Commands**: `_split_tmp\run_phase1b.cmd` (or the equivalent `gh repo list WFP-VAM --json name,visibility,viewerPermission`, `gh api repos/WFP-VAM/VAM_LLM/branches/main/protection`, `git rev-parse` of the pointer commits, `git merge-base --is-ancestor wfp/main phase_3_faster_MFI`), then `git status --porcelain` in R2 and R3 must be identical to section 1.
- **Expected**: already answered on 2026-09-22 10:39 (no org repo for MarketAIssist; `main` unprotected; ancestry confirmed) — step 0 only re-confirms that nothing changed since, especially `git status` and `ls-remote` of the three remotes.
- **Undo**: nothing to undo. **Stop condition**: any new uncommitted change in R2/R3 that is not in section 1.

### Step 1 — Safety tags
- **Repo**: R2, R3. **Commands**:
  - R2: `git tag -a pre-split-20260922 c3b35d3 -m "State before the VAM LLM / MarketAIssist repo split"`; `git push origin pre-split-20260922`; `git push wfp pre-split-20260922` (the tag also lands on the public org repo — harmless, it points at a commit that is already public on `edacrema/VAM-UNIFIED-APP`)
  - R3: `git tag -a pre-split-20260922 48c9fd3 -m "State before the VAM LLM / MarketAIssist repo split"`; `git push origin pre-split-20260922`
- **Verify**: `git ls-remote --tags <remote>` shows the tag on each remote. **Undo**: `git tag -d pre-split-20260922` and `git push <remote> :refs/tags/pre-split-20260922` (only if Eugenio wants them gone; tags are harmless).

### Step 2 — Record the baseline
- Copy section 4 numbers into this document (already done by Phase 1); keep `_split_tmp/phase1_tests.log` until the end. No repo change.

### Step 3 — Freeze both venvs (needed later for the move)
- **Repo**: R2, R3. **Commands**: `.\venv\Scripts\python.exe -m pip freeze > ..\..\_split_tmp\venv-freeze-<repo>-20260922.txt` (written outside the repos). Rationale: `pytest`, `pytest-asyncio`, `pytest-benchmark`, `pytest-socket`, `pytest-recording` are installed but **not in `requirements.txt`**; the recreated venvs must get them back. **Undo**: none needed.

### Step 4 — MarketAIssist working branch and data-dir hygiene
- **Repo**: R3. **Commands**: `git switch -c split/align-shared main`; append `outputs/` and `test datasets/` to `.gitignore`; `git add .gitignore`; `git commit -m "chore: ignore local outputs and test datasets"`.
- **Verify**: `git status --porcelain` is empty (the two dirs disappear from untracked). **Undo**: `git switch main; git branch -D split/align-shared`.

### Step 5 — (R3) nothing to port for `streamlit_shared.py`, `async_runs.py`, `gcs.py`, `countries.py`, `__init__.py` — recorded as verified, no commit.

### Step 6 — Port `databridges.py` hardening into R2 (on the R2 split branch)
- **Repo**: R2. **Commands**: `git switch -c split/remove-validators phase_3_faster_MFI`; copy `MarketAIssist/app/shared/databridges.py` over `app/shared/databridges.py` (EOL is irrelevant for git thanks to `core.autocrlf=true`; the working-tree copy can stay as is); update `tests/test_databridges_client.py`: introduce `CLIENT_ID = "00000000-0000-4000-8000-000000000001"` and use it wherever `"client-id"`, `"key"`, `"old-key"`, `"new-key"` are passed as the API key (secrets can stay arbitrary strings); add the two negative tests from R3 (`not-an-azure-client-uuid` → `ValueError`, `…:latest` → `ValueError`). `git diff --stat` must show only these two files. Commit: `feat(databridges): reject secret-manager references and non-UUID client ids` (one commit, file + its test).
- **Verify**: `.\venv\Scripts\python.exe -m pytest tests\test_databridges_client.py tests\test_databridges_adapter.py -q` green; `git diff pre-split-20260922 -- app/shared/databridges.py` equals the 33-line diff of section 3; `python -c "import app.shared.databridges"` OK with no env vars.
- **Undo**: `git reset --hard pre-split-20260922` on the branch (branch not yet pushed).

### Step 7 — Port `llm.py` into R3
- **Repo**: R3 (branch `split/align-shared`). **Commands**: copy `UNIFIED APP/app/shared/llm.py` over `app/shared/llm.py` (EOL irrelevant for git, see 1.3); add trimmed `tests/test_llm_runtime_config.py` (no env → defaults `gemini-2.5-pro`/`us-central1`/90 s/2 retries; invalid `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES`, `LLM_MAX_OUTPUT_TOKENS` → `LLMRuntimeConfigurationError` with code `llm_runtime_configuration_invalid`; `get_model()` with no arguments still works with `ChatVertexAI` monkeypatched); add to `.env.example` the optional `LLM_TIMEOUT_SECONDS=` and `LLM_MAX_RETRIES=` lines (names only, commented with defaults). Commit: `feat(llm): adopt validated runtime configuration from VAM LLM`.
- **Verify**: `.\venv\Scripts\python.exe -m pytest tests -q` green; `python -c "from app.shared import llm; print(llm.llm_runtime_config())"` with `LLM_*`/`MFI_*` unset prints the defaults; `cmp` (EOL-insensitive) of the two `llm.py` files → identical.
- **Undo**: `git reset --hard main` on the branch.

### Step 8 — MarketAIssist README and `.env.example`
- **Repo**: R3. **Commands**: edit `README.md`: add "What this repo is / is not" (validators only; drafters live in VAM LLM), "Origin" (derived from UNIFIED APP commit `524cc9a`, repo `https://github.com/WFP-VAM/VAM_LLM`; root commit `d810efd`), link to the VAM LLM repo; keep the existing deployment sections. Commit: `docs: describe repo scope and origin after the split`.
- **Verify**: rendered README reads correctly; `git status` clean. **Undo**: `git revert <sha>`.

### Step 9 — Remove the validators from R2 (branch `split/remove-validators`, several small commits)
- 9a `refactor(api)!: drop MFI/Price validator routers` — `main.py` (imports, routers, services list, title/description) + `git rm -r app/services/mfi_validator app/services/price_validator`. Verify: `python -c "import main"`; `python -c "from fastapi.testclient import TestClient; import main; print(TestClient(main.app).get('/').json())"` → 3 services.
- 9b `refactor(dispatcher): remove validator dispatch paths` — `dispatcher.py` imports L48/L71, block L581–1133, services list L2831–2832, routing L2856–2873. Verify: `python -c "from app.streamlit_backend.dispatcher import dispatch_request as d; print(d('GET','/').status_code, d('GET','/mfi-validator/info').status_code, d('GET','/price-validator/info').status_code)"` → `200 404 404`.
- 9c `refactor(ui): remove validator pages and landing buttons` — `git rm pages/1_MFI_Validator.py pages/2_Price_Validator.py app/shared/assets/mfidata_validator.jpeg app/shared/assets/pricedata_validator.jpeg`; `streamlit_app.py` L109–133 → three-button layout. Verify: `python -c "from streamlit.testing.v1 import AppTest; at=AppTest.from_file('streamlit_app.py', default_timeout=60); at.run(); assert not at.exception; print([b.label for b in at.button])"` → onboarding + 3 drafters + instructions.
- 9d `test: drop Price Validator test, add split guards` — `git rm tests/test_price_validator_market_names.py`; add `tests/test_split_guards.py` (dispatcher returns 404 for `/mfi-validator/*` and `/price-validator/*`; FastAPI `/` lists exactly `market-monitor`, `mfi-drafter`, `seasonal-outlook`). Verify: full `pytest tests -q` → baseline − 7 + 2, all green; `python scripts/check_mfi_reliable.py` green.
- 9e `docs: rewrite tester-facing pages for the three drafters` — `pages/1_How_To_Use_The_Tools.py`, `pages/0_Tester_Onboarding.py` (with the MarketAIssist pointer). Verify: `AppTest.from_file('pages/1_How_To_Use_The_Tools.py').run()` without exception; grep of the four identifiers on `pages/` empty.
- 9f `docs: rewrite app overview for VAM LLM only and add README` — `docs/app-overview.md` (tracked, so no `-f` needed) + new `README.md` (what it is / is not; validators moved to MarketAIssist at `<org>/MarketAIssist`, derived from commit `524cc9a`; link) + 3-line note at the top of `evals/eval_framework_overview.md`.
- 9g `chore: drop validator-only configuration` — `.env.example` L36; `market_monitor/data_loader.py:154` wording; optional: dead helpers in `app/shared/gcs.py` (D8), `chardet`/`openpyxl` in `requirements.txt` (D8).
- **Acceptance for step 9** (all on the branch): `git grep -n -I -E "mfi_validator|price_validator|mfi-validator|price-validator" -- . ':!specs' ':!evals'` → **no output**; `git status --porcelain` → only the pre-existing untracked paths of 1.3; no file under `MFI Test Databases/` or `app/price_data.csv` staged (`git diff --cached --stat` never lists them).
- **Undo** (any sub-step): `git reset --hard <previous commit>` while unpushed; after push, `git revert`.

### Step 10 — Publish the split branch and open the PR
- **Repo**: R2. Prerequisite: the working branch must exist on the canonical remote first: `git push wfp phase_3_faster_MFI` (new branch on WFP-VAM/VAM_LLM, no history rewrite) and `git push wfp phase_3` (fast-forward of `wfp/phase_3`, 3 commits). Then `git push -u origin split/remove-validators` (backup) and `git push wfp split/remove-validators`.
- `gh pr create -R WFP-VAM/VAM_LLM --base phase_3_faster_MFI --head split/remove-validators --title "Remove MFI/Price validators (moved to MarketAIssist)" --body-file _split_tmp/pr_body.md` (body = summary of section 2 + test delta; ends with the attribution lines required by this session).
- **Verify**: `gh pr checks`/`gh pr view` show the PR against `phase_3_faster_MFI`; diff stat matches steps 6+9. **Undo**: `gh pr close`; the branch can be left or deleted locally (`git branch -D`) — remote branches are never deleted by this plan.

### Step 11 — Merge (after Eugenio's review) and sync
- `gh pr merge <n> --merge` (merge commit, keeps the small thematic commits; **no squash, no rebase**). Then in R2: `git switch phase_3_faster_MFI; git pull --ff-only wfp phase_3_faster_MFI; git push origin phase_3_faster_MFI` (backup mirror of the same commits).
- **Verify**: `git log --oneline -3` shows the merge; `git rev-parse phase_3_faster_MFI origin/phase_3_faster_MFI wfp/phase_3_faster_MFI` → same hash; full test suite green on `phase_3_faster_MFI`.
- **Undo**: `git revert -m 1 <merge sha>` (never a force-push).

### Step 12 — MarketAIssist: PR, merge, canonical remote on WFP-VAM
- **Repo**: R3. The org repo does not exist (verified 10:39): `gh repo create WFP-VAM/MarketAIssist --private --description "AI tools to validate MFI and price datasets before DataBridges submission" --disable-wiki` (name/visibility per D2/D3). Then `git remote add wfp https://github.com/WFP-VAM/MarketAIssist.git`; `git push wfp main`; `git push wfp pre-split-20260922`; `git push -u origin split/align-shared`; `git push wfp split/align-shared`; `gh pr create -R WFP-VAM/MarketAIssist --base main --head split/align-shared …`; `gh pr merge --merge`; `git switch main; git pull --ff-only wfp main; git push origin main`.
- **Verify**: `gh repo view WFP-VAM/MarketAIssist --json defaultBranchRef,visibility`; `git ls-remote wfp` lists `main` + tag; tests green on `main`.
- **Undo**: a freshly created org repo can be deleted only by Eugenio in the GitHub UI (`gh repo delete` needs the `delete_repo` scope the token does not have) — hence D2/D3 must be settled before this step.

### Step 13 — Make WFP-VAM the canonical remote name (`origin`)
- **Repo**: R2: `git remote rename origin personal; git remote rename wfp origin; git branch -u origin/phase_3_faster_MFI phase_3_faster_MFI; git branch -u origin/phase_3 phase_3`. **Do not** touch local `main`'s upstream unless D5 says so. R3: `git remote rename origin personal; git remote rename wfp origin; git branch -u origin/main main`.
- **Verify**: `git remote -v`; `git status` says "up to date with origin/…"; `git fetch --all --dry-run` succeeds. **Undo**: rename back (pure local metadata). (Alternative if D6 prefers: keep names as they are and only document which one is canonical.)

### Step 14 — Move the folders out of `vam-llm\`
- Preconditions: steps 10–13 done, both `git status` clean, **no editor/terminal/Streamlit/Docker process open inside the two folders** (a rename fails on any open handle), `_split_tmp/venv-freeze-*.txt` present.
- **Commands** (PowerShell, from `C:\Users\eugen\VAM LLM Materials`):
  1. `Remove-Item -Recurse -Force '.\vam-llm\VAM LLM - Prototypes\UNIFIED APP\venv'` and the same for `MarketAIssist\venv` (venvs only; nothing else is deleted).
  2. `Move-Item '.\vam-llm\VAM LLM - Prototypes\UNIFIED APP' '.\vam-llm-app'` and `Move-Item '.\vam-llm\VAM LLM - Prototypes\MarketAIssist' '.\marketaissist'` — same volume, so this is an atomic directory rename: `.git`, `.env`, `MFI Test Databases/`, `test datasets/`, `outputs/`, `.tmp/` (even its access-denied sub-dirs) all travel untouched; nothing is copied file by file.
  3. `git config --global --add safe.directory 'C:/Users/eugen/VAM LLM Materials/vam-llm-app'` (the existing entry points at the old UNIFIED APP path; harmless to leave).
  4. Recreate venvs: `C:\Python312\python.exe -m venv venv` then `.\venv\Scripts\python.exe -m pip install -r ..\vam-llm\_split_tmp\venv-freeze-<repo>-20260922.txt` (exact reproduction, includes pytest plugins and the `git+https://github.com/WFP-VAM/DataBridgesAPI.git@v8.0.0` pin for R2); fallback `pip install -r requirements.txt pytest pytest-asyncio pytest-benchmark` if the freeze fails on a wheel.
  5. `git status` in both new locations; `python -m pytest tests -q` in both; `python scripts/check_mfi_reliable.py` in `vam-llm-app`.
- **Verify**: `Get-ChildItem 'vam-llm-app\MFI Test Databases' | Measure-Object` = 3 files; `Get-ChildItem 'marketaissist\test datasets'` = 16 files; `.env` present in both (size 590 B / 496 B, not opened); `git -C vam-llm status` no longer lists `UNIFIED APP/` or `MarketAIssist/` as untracked; test counts equal step 11/12.
- **Undo**: `Move-Item` back to the original paths (same atomic rename); venvs are disposable.
- Absolute-path check done in Phase 1: **no tracked or config file (`.claude/`, `.agents/` (empty), `.vscode/`, `.streamlit/`, scripts, `*.toml/json/yaml/cfg/ini/bat/ps1/sh`) contains `C:\Users\eugen` paths** in either repo; the only hits are the untracked draft `MFI_analysis_language_style_discussion_draft.md` (links to local PDFs, moves with the folder) and old logs under `.tmp/`.

### Step 15 — (Optional, D10) note in the archive repo `vam-llm`
- **Repo**: R1. Add `README.md` (2 paragraphs: this is the notebook/MVP archive; the apps live at `<org>/VAM_LLM` and `<org>/MarketAIssist`, local folders `vam-llm-app` and `marketaissist`). Commit on `DEV` only if Eugenio agrees; the pre-existing unstaged notebook changes stay untouched (commit with an explicit path: `git add README.md; git commit -m "docs: point to the VAM LLM and MarketAIssist app repositories"`). Push `DEV` only if Eugenio also wants the pre-existing "ahead 1" commit pushed (D10).

### Step 16 — Cleanup and final acceptance
- Delete `C:\Users\eugen\VAM LLM Materials\vam-llm\_split_tmp\` (scripts, logs, freezes) after Eugenio has a copy of the two logs if he wants them; run the full section-9 acceptance checklist once more from the new locations; update `context/*.md` in the Claude project with the new paths and remotes.

---

## 6. Risks (probability / impact / mitigation)

| # | Risk | P | I | Mitigation |
|---|---|---|---|---|
| R-1 | Residual import breaks Streamlit or FastAPI start after removal (e.g. a lazy `from app.services.price_validator…` inside a function) | Low — full-tree grep found all imports (`dispatcher.py:48,71,877`, `main.py:6–7`, the two pages, one test) | High | Step 9 verifies with real imports: `TestClient(main.app)`, `dispatch_request` smoke, `AppTest` on `streamlit_app.py` and every page; acceptance grep before merge |
| R-2 | Tests fail after removal for reasons other than the intended −7 | Low | Medium | Baseline 1293 recorded; only `test_price_validator_market_names.py` imports validator code; no test inspects the services list; run the full suite after each sub-step of 9 |
| R-3 | Ported `databridges.py` makes R2 tests fail (non-UUID dummy keys) | **Certain if the test file is not adapted** | Medium | Step 6 ports file + test together in one commit; `test_databridges_adapter.py` (price cache) is unaffected because it uses `WfpApiToken` |
| R-4 | Ported `llm.py` breaks MarketAIssist with no env vars | Low | High | Defaults verified by reading both files: same model/location/retries, timeout 60→90 s; new test pins the defaults; smoke `llm_runtime_config()` with a clean env; only risk is an invalid value already present in Eugenio's local `.env` for `LLM_MAX_OUTPUT_TOKENS` (would now raise with a clear message — check by running the app once) |
| R-5 | Loss of local datasets during the move | Very low | Very high | Same-volume `Move-Item` = directory rename, no per-file copy; venv deleted **before** the move is the only deletion; explicit file counts before/after; `MFI Test Databases/`, `test datasets/`, `.env` verified present after |
| R-6 | Move fails because of open handles or the access-denied `.tmp` sub-dirs | Medium | Low | Close VS Code/terminals on those folders first; a rename does not enumerate children, so access-denied sub-dirs do not block it; if it still fails, retry after reboot — never fall back to copy+delete |
| R-7 | `origin` vs `wfp` conflicts in R2 | Low | Medium | Only fast-forward pushes are planned (`wfp/phase_3` +3, new `wfp/phase_3_faster_MFI`); local `main` is 49 behind `wfp/main` and is **never pushed**; every push names one branch; no `--force` |
| R-8 | Recreated venvs miss packages (pytest plugins are not in `requirements.txt`; R2 needs the git-pinned DataBridges client) | Medium | Medium | `pip freeze` snapshot in step 3, reinstall from it; test counts compared with step 11/12 |
| R-9 | Cannot create the org repo / PR (permissions) | Low — `members_can_create_repositories=true`, ADMIN on VAM_LLM | Medium | Step 0 re-checks; fallback: Eugenio creates the empty repo in the GitHub UI, the plan continues from `git remote add` |
| R-10 | Accidentally committing real data in R3 (`test datasets/`, `outputs/` are not ignored) | Medium today | High | Step 4 adds them to `.gitignore` before any other commit; every commit uses explicit paths, never `git add .` |
| R-11 | Mixed CRLF/LF in the working trees produce noisy diffs | Low — `core.autocrlf=true`, index is LF in both repos | Low | Review with `git diff` (already normalised); expect harmless "LF will be replaced by CRLF" warnings on the `w/mixed` files; a `.gitattributes` is out of scope |
| R-12 | The public `WFP-VAM/VAM_LLM` receives commits that mention internal URLs | Low | Low | Nothing new is added beyond what `phase_3_faster_MFI` already contains on the public personal repo; no `.env` content is ever touched |

---

## 7. Decisions needed from Eugenio (not covered by section 3 of the brief)

- **D1 — Folder names**: proposal `C:\Users\eugen\VAM LLM Materials\vam-llm-app` and `C:\Users\eugen\VAM LLM Materials\marketaissist` (siblings of `vam-llm`). Alternative: `VAM_LLM` to mirror the org repo name.
- **D2 — Org repo name for MarketAIssist**: no existing org repo (verified); proposal `WFP-VAM/MarketAIssist` (same as the personal repo). Note the org's naming is inconsistent anyway (`vam-llm` vs `VAM_LLM`).
- **D3 — Visibility of the new org repo**: the personal `edacrema/MarketAIssist` is private, `WFP-VAM/VAM_LLM` is public. Proposal: **private** (can be opened later; the reverse is not clean).
- **D4 — Which R2 branches go to WFP-VAM**: proposal only `phase_3_faster_MFI` (working), `phase_3` (fast-forward) and the split PR branch; `main` is not touched (see D5). The six dead branches are **not** pushed to the org.
- **D5 — What to do with `main`**: (a) leave `wfp/main` at `02dd3c9` and local `main` at `83d16c3` (nothing changes, but `main` on the canonical repo is a stale Aug-26 snapshot and local `main` is even older); (b) fast-forward local `main` to `wfp/main` now (`git switch main; git merge --ff-only wfp/main; git push origin main`) — safe, personal `origin/main` moves forward by fast-forward; (c) after the PR merge, also fast-forward `wfp/main` to the post-split `phase_3_faster_MFI` so that `main` becomes the release line. Proposal: (b) now, (c) only if Eugenio wants `main` to mean "current".
- **D6 — Remote naming**: rename so that `origin` = WFP-VAM and `personal` = edacrema (step 13, proposal) vs keep `origin`/`wfp` and just document. Purely local, reversible.
- **D7 — `pages/1_How_To_Use_The_Tools.py` scope**: the page has no Seasonal Outlook section. Proposal: add a short "3. Seasonal Outlook Drafter" section (what it does / how to use / output, derived from `pages/5_Seasonal_Outlook_Drafter.py` and `specs/seasonal_outlook_implementation.md`) so the page really presents three tools; alternative: leave a one-line placeholder and keep the rewrite to pure removal.
- **D8 — Optional cleanups in R2**: remove the dead `get_market_names_*` helpers from `app/shared/gcs.py`; remove `chardet` and `openpyxl` from `requirements.txt`. Proposal: do the `gcs.py` cleanup (tiny, tested), leave `requirements.txt` for a later, deploy-aware change.
- **D9 — Untracked files in R2**: `specs/aws_step_functions_assessment.md`, `evals/mfi_analysis_style_prompt_review.md`, `MFI_analysis_language_style_discussion_draft.md`, `MFI_Drafter_2_0_workflow.html`, `MFI Manuals/`, `workflow_slides/` (with 7,778 node_modules files). Proposal: leave them untracked and untouched (they move with the folder); consider adding `workflow_slides/.build/` and `MFI Manuals/` to `.gitignore` in a separate commit only if Eugenio wants a quiet `git status`. Commit the two markdown assessments only on explicit request.
- **D10 — Archive repo note** (step 15) and whether to push the pre-existing unpushed `DEV` commit of `vam-llm`.
- **D11 — Dead local branches** (`phase_3_complex`, `phase_3_ver1`, `prov_1`, `docker-streamlit`, `docker-streamlit-only`, `local-streamlit`): proposal: keep them locally and on the personal `origin` (backup) — nothing is deleted by this plan; local deletion of the five fully-merged ones can be a later housekeeping step (`git branch -d`, refuses if unmerged).
- **D12 — R3 workflow**: branch + PR (`split/align-shared`, proposal) or direct commits on `main`.
- **D13 — Verbatim vs trimmed `llm.py` in R3**: verbatim copy (proposal; files stay byte-comparable at split time, the two `MFI_*` settings are inert) vs a trimmed version without the MFI-specific fields.

---

## 8. Explicitly out of scope

Deployment and Cloud Run (images `unified-app`/`marketaissist-app`, the hard-coded `INSTRUCTIONS_PAGE_URL` Cloud Run hostname in R2's `streamlit_shared.py`, service names such as `vam-llm-marketaissist-…`), Terraform under `deploy/`, IAM/GCP changes, communication to testers, the AWS migration, the shared Firestore database name `vam-llm-async`, `.gitattributes`/EOL normalisation, removal of `refs/codex/*`, cleanup of `.tmp/` (3.4 GB), the pre-existing uncommitted notebook changes in `vam-llm`, and any change to the personal `edacrema/*` repositories beyond receiving the same branches/tags as backups.

---

## Appendix A — What Phase 1 actually ran (all read-only)

`_split_tmp/phase1_status.ps1` (git status/branch/remote/log/stash/worktree/tag/for-each-ref/ls-remote/config per repo; `git grep --untracked` inventories; `git diff --no-index` of the shared files and validators; `git log --find-object` for the history pointer; `gh auth status`, `gh api user/memberships/orgs/WFP-VAM`, `gh repo view` ×4; venv/pip listings) `_split_tmp/phase1_tests.ps1` (`pytest --collect-only`, `pytest tests -q`, `scripts/check_mfi_reliable.py`, `import main` route listing) and `_split_tmp/phase1b_gh.ps1` (`gh repo list` for both accounts, branch lists and protection via `gh api`, `git rev-parse` of the pointer commits, `git merge-base --is-ancestor`, `git ls-files --eol`). No `.env` was read; no repo file was modified; the only files created are under `vam-llm\_split_tmp\` and this document.
