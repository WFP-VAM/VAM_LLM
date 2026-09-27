# Drafter layout: one file layout for the three drafters

**Status: approved 26 September 2026 (the user's four layout decisions, then this plan and three further decisions, §9). Phases 0 to 3 (MFI, Seasonal Outlook, Market Monitor) done on `refactor/drafter-layout` (not pushed); Phase 4 (close-out) waits for the go-ahead.** Based on `refactor/shared-layer` @ `68cec22`, after the shared-layer rationalization (`shared_layer_rationalization.md`). Release gate unchanged (rule D9 of that plan): nothing is merged or deployed before the user accepts Phase 7 of the coherence refactor on GCP and releases the D8 fixes. This refactor ships after the shared-layer one.

---

## 1. Summary

The three drafters are LangGraph graphs laid out three different ways:

- **Market Monitor (MM)** keeps everything in one 3,458-line `graph.py`: model-call plumbing, state, ~660 lines of helpers, QA helpers, the four optional report modules, the ten nodes, the graph and the public entry point.
- **MFI** keeps its drafting nodes as closures inside `light_graph.build_graph`, its prompt texts mixed with response schemas in `light_contracts.py`, and every module name carries a `light_` prefix.
- **Seasonal Outlook** already has a small `graph.py`, but every stage runs through `if stage == …` branches in `engine.py`, and its prompts are split over two files whose names clash (`COMMON`, `REVIEW`, `prompt`).

After this refactor, each drafter has:

- **`graph.py`**: the graph only (state where nodes don't need it, node wiring, edges, routing, compile);
- **`nodes/`**: one module per node **type** (MFI's draft/review/correct serve both families; Seasonal has one per stage plus `export`; MM one per node);
- **`prompts.py`**: the prompt texts and the functions that assemble them;
- **purpose-named support modules** for what several nodes share (no `utils.py`).

Moves, renames and import rewiring only. Every prompt byte, node and stage name, operation name, stored field, API reply, Word file, page and dependency stays identical, and each commit proves it (§8).

## 2. Invariants

Unchanged, and checked on every commit:

- **Graph node and stage names** (run records' `current_node`, the MM router's `progress_map` and `on_step` branches, the MFI router's `on_step`, `light_phases`, Seasonal `stages`/`completed`, the pages).
- **LLM operation names and work-item ids:** `market_monitor.*.v1`, `mfi.light.*.v1`, `seasonal_outlook.{stage}.v1`, MFI's `kind + ":" + fingerprint(...)`, Seasonal's `f"{namespace}_{name}"`.
- **Workflow, contract and prompt identifiers:** `mfi-light-v1`, `mfi-light-contracts-v1`, the `light_phases` and `light_narrative` fields, Seasonal's `WORKFLOW_REVISION`, `seasonal_evidence_v1`/`seasonal_report_v1`, `seasonal-prompts-v1`.
- **Every prompt byte sent to the model**, and MFI's `effective_contract()` (schema and prompt hashes).
- **Stored data shapes, API responses, Word files, report blocks, live outputs, UI, environment variable names, dependencies.**
- **Tests:** only import paths, monkeypatch targets, and the source paths that two guard tests read (§6.3). No assertion changes.

**One accepted difference (decided 26 September):** logger names follow the modules. Each module keeps `logging.getLogger(__name__)`, and `LOG_FORMAT` prints `%(name)s`, so a log line from MM's event mapper reads `app.services.market_monitor.nodes.event_mapper - INFO - [EventMapper] …` instead of `app.services.market_monitor.graph - INFO - …`. Message texts do not change. No test, config or alert in the repository depends on a logger name.

## 3. Current state (at `68cec22`)

### 3.1 Market Monitor (`app/services/market_monitor/`)

`graph.py` (3,458 lines), top to bottom: imports and model-call plumbing (`llm_provider`, `llm_client`), `CURRENCY_SYMBOLS`, `TERMINOLOGY_THRESHOLDS`; `MarketReportState` and `create_initial_state`; "utility functions" (language, prompt JSON, basket context, formatting, chart helpers); QA and correction helpers; `ReportModule` and its four modules; the nodes with their helpers; routing and `build_graph`; `run_report_generation`.

- Seven output-facing prompts are templates in `prompts/{en,fr,es}/*.txt` with a hash manifest, read through `prompt_registry.py`. Two English-only prompts are inline f-strings in `node_event_mapper` and `node_trend_analyst`.
- Importers: `router.py` (`run_report_generation`, `AVAILABLE_MODULES`, `normalize_qa_review`), `__init__.py` (lazy exports looked up on `.graph`), `scripts/phase7_second_basket_qa.py` (`node_graph_designer`), four test files (`test_market_monitor_graph_fx`, `test_market_monitor_i18n`, `test_market_monitor_phase6`, `test_seerist_retrieval`) with 32 distinct `market_graph.<name>` references, and the `.tmp` tools.

### 3.2 MFI (`app/services/mfi_drafter/`)

- `light_graph.py`: `State`, `prepare_analysis`, `retrieve_context`, `render_figures`, `texts`, and `build_graph(ledger, llm, notify)`, whose closures `package_for`, `generate_family` (with the `Oversized` split), `correction`, `synthesis` and `assemble` are the drafting nodes. Eleven nodes, `charts` in parallel with the drafts.
- `light_contracts.py`: identifiers and limits, response schemas and their provider compiler, `parse_response`, `inspect_sections`, **and** the policy texts and `instructions(kind)`.
- `light_service.py`, `light_runtime.py`, `light_evidence.py`, `light_report.py`; `context.py` (`retrieve_context_documents`, used only by the context node).
- Importers: `__init__.py` (lazy `.light_service`, `.light_graph`), `router.py`, eight test files, the `.tmp` tools.

### 3.3 Seasonal Outlook (`app/services/seasonal_outlook/`)

- `graph.py` (50 lines): one graph, an entry per phase following `engine.CHAINS`; every LLM stage is the generic `stage(name)` closure (`engine.request_for` → `recorder.prepare` → `llm.generate` → `engine.accept`); the `export` node calls a builder the runner injects.
- `engine.py` (105 lines): `EVIDENCE_STAGES`, `CHAINS`, `initial_state`, `evidence_context`, and all per-stage logic as branches of `request_for` and `accept`.
- Prompts: `science/evidence_prompts.py` and `science/report_prompts.py`.
- Importers: `graph.py`, `service.py` (`initial_state`, `CHAINS`), `tests/test_seasonal_outlook.py` (`engine.initial_state`, `engine.request_for`, `engine.accept`), `.tmp/shared-layer/wire_seasonal.py`.

## 4. Target layout

### 4.1 MFI

```
app/services/mfi_drafter/
  graph.py                 State; build_graph(ledger, llm, notify): stage() wrapper, nodes bound with lambdas
  nodes/__init__.py        docstring only
  nodes/prepare_analysis.py    prepare_analysis(inputs)
  nodes/context_retrieval.py   TOPIC_TERMS, retrieve_context_documents(state), retrieve_context(base)
  nodes/charts.py              render_figures(base)
  nodes/draft.py               draft_family(runtime, ledger, s, f, n)
  nodes/review.py              review_family(runtime, ledger, s, f, n)
  nodes/correct.py             correction(runtime, ledger, s, f, n)
  nodes/executive_summary.py   synthesis(runtime, s)
  nodes/assemble_report.py     assemble(s)
  sections.py              texts, package_for, generate_family(runtime, ledger, state, family, kind)
  prompts.py               POLICY … MARKET_GUIDANCE, instructions(kind)
  contracts.py             identifiers, limits, NODES, schemas, provider-schema compiler, dumps, parse_response, inspect_sections
  runtime.py  service.py  evidence.py  report.py      (renamed from light_*)
  render_worker.py         unchanged, same path (subprocess `-m app.services.mfi_drafter.render_worker`, parents[3])
```

- `graph.py` imports the node modules under aliases (`from .nodes import draft as draft_node, …`): `build_graph` has loop locals named `draft`, `review`, `correct`. It calls `module.function` inside the lambdas, so a test patches the node module. The `f=family, n=…` default-argument capture stays on all three per-family bindings, including `correction`. `kind` stays the full node name (`"draft_dimensions"`), which feeds `instructions(kind)`, the work-item id and the `startswith` branches.
- LangGraph 1.x injects node parameters named `runtime`, `config`, `store`, `writer` and `previous`. The node functions that take `runtime` are only ever called from the lambdas inside `stage()`, never registered directly.
- `graph.py` keeps no module-level state: per-run dependencies (`runtime`, `ledger`) are arguments, bound in `build_graph`.
- Relative imports inside moved functions gain a level in `nodes/` (`from ..render_worker import …`) and stay lazy where they were lazy.
- The router keeps its alias `runtime_status as light_runtime_status`. It is a name inside the router, which a test calls, not a module name.

### 4.2 Seasonal Outlook

```
app/services/seasonal_outlook/
  graph.py              per-phase entries and the stage(name) wrapper unchanged; request_for/accept from .nodes
  nodes/__init__.py     STAGES = {name: module}; request_for(stage, state); accept(stage, state, response, namespace)
  nodes/extraction.py  nodes/review.py  nodes/refinement.py  nodes/feedback.py
  nodes/draft.py  nodes/report_review.py  nodes/redraft.py
                        request(state) and accept(state, response, namespace): that stage's branch code
  nodes/export.py       export(state, build) -> {'artifacts': build(state)}  (the builder is still injected)
  engine.py             EVIDENCE_STAGES, CHAINS, initial_state, evidence_context, and the shared stage pieces:
                        evidence_inputs, evidence_images, stage_request, parse_reply, check_map_dates
  prompts.py            the two science prompt files merged
  exports.py            unchanged (service.py also builds the input artifact with package())
```

- **Why `engine.py` stays:** `service.py` and the tests import `CHAINS` and `initial_state` from it, and its docstring role (pure stage preparation and validation, separate from durable execution) still describes the shared pieces. A new name would only add churn.
- **Why the dispatcher is in `nodes/__init__.py`:** the tests call `request_for(stage, state)` and `accept(stage, …)` by stage name ten times. With the dispatcher next to the stage modules, those tests change only their import, and `engine.py` never imports `nodes` (no cycle).
- **Order is kept where it is observable.**
  - `calls.llm_request` dumps the payload without `sort_keys`, so the payload keys keep their order: case → initial/latest_extraction → visual_review → analyst_comments, and case → evidence → initial_analysis → draft_review. `figure_ids` stays set in place on `case`, and extraction keeps its `if previous:`.
  - The validation message is recorded, so each stage keeps the original order of its checks.
  - The key order of the request and result dicts is not observable: `encode` and `digest` sort keys.
- The report stages resolve the schema choice statically: draft and redraft use `draft_schema`, report_review uses `review_schema`.
- A stage name outside the seven now raises `KeyError` in the dispatcher, where it used to fall into the report branch. The graph only ever passes the seven.

### 4.3 Market Monitor

```
app/services/market_monitor/
  graph.py            OnStepCallback, MAX_CORRECTION_ATTEMPTS, should_correct, build_graph (wrap_node, edges, compile)
  service.py          run_report_generation (new module; decided 26 September)
  state.py            MarketReportState, create_initial_state, _state_language, _state_currency_code
  runtime.py          llm_provider, llm_client (ContextVar tracer unchanged; explicit wiring stays a follow-up)
  text.py             generated-prose normalisation, localised numbers, short-text de-duplication
  prompts.py          prompt_registry.py renamed (same functions), plus the prompt inputs and the two inline prompts
  prompt_templates/   prompts/ renamed with git mv (bytes and manifest untouched)
  basket_context.py   the immutable basket facts every prompt receives
  qa.py               the QA and correction helpers
  modules.py          ReportModule, the four optional modules, AVAILABLE_MODULES, CURRENCY_SYMBOLS
  nodes/__init__.py   docstring only
  nodes/data_agent.py  graph_designer.py  news_retrieval.py  event_mapper.py  trend_analyst.py
  nodes/module_orchestrator.py  highlights_drafter.py  narrative_drafter.py  red_team.py  prepare_correction.py
```

- **Import direction** (checked from the AST; no cycle, no node→node and no node→graph edge): `state` ← `text`, `prompts`, `basket_context`; `prompts` ← `qa`; `state`, `prompts`, `text`, `qa`, `basket_context` ← `modules`; all of them and `runtime` ← `nodes/*` ← `graph` ← `service`. `router.py` imports `service`, `modules` and `qa`; `__init__.py` looks names up on their new modules.
- **`service.py`** (decided 26 September): `graph.py` holds the graph only, as in MFI and Seasonal, whose public entry points are in their `service.py`. It imports `build_graph` at module level, so the one test that patches it targets `service.build_graph`.
- **Chart helpers** (decided 26 September): the 27 chart helpers have one user, `node_graph_designer`, so they live in `nodes/graph_designer.py` (about 870 lines). The tests that patch a helper and call the node use one module.
- **`prompts.py`** gets the prompt inputs `TERMINOLOGY_THRESHOLDS`, `_json_for_prompt` and `_report_month_for_prompt`, and two builders holding the inline f-strings verbatim, still as f-strings:
  - `event_extraction_prompt(country, context)`;
  - `trend_analysis_prompt(stats, events, basket_context)`. It keeps `json.dumps(…, indent=2)` for the statistics, events and thresholds (which escapes non-ASCII) and `_json_for_prompt` for the basket context (which doesn't).
  - The directory rename avoids a `prompts.py` next to a `prompts/` directory. `PROMPT_ROOT` becomes `Path(__file__).with_name("prompt_templates")`.
- **`qa.py` keeps the whole QA block**, including `_targeted` (only highlights uses it) and `qa_review_from_state` (only red_team). They share one vocabulary of section ids and severities and move verbatim as one block.
- `state.py` imports `Annotated`, `operator` and the typing names it annotates with. Because of `from __future__ import annotations`, a missing one would only fail when `StateGraph()` is built: the suite catches that, `import_all` does not.
- **Names stay identical, private ones included:** the tests reference them.

## 5. Move map

### 5.1 Market Monitor `graph.py` → new modules (90 top-level names, all mapped)

| New module | Names (line in `graph.py` @ 68cec22) |
|---|---|
| `state.py` | `MarketReportState` (118), `create_initial_state` (186), `_state_language` (271), `_state_currency_code` (411) |
| `runtime.py` | `llm_provider` (68), `llm_client` (73) |
| `text.py` | `_normalize_output_text` (283), `_validated_prose` (289), `_plain_or_localized_number` (296), `format_pct` (302), `_dedupe_text` (371) |
| `prompts.py` | `TERMINOLOGY_THRESHOLDS` (104), `_json_for_prompt` (275), `_report_month_for_prompt` (279); new builders `event_extraction_prompt` (f-string of 2615), `trend_analysis_prompt` (f-string of 2694) |
| `basket_context.py` | `_mapping` (418), `_prompt_scope_label` (422), `_prompt_metric_payload` (430), `_prompt_component_payload` (457), `_basket_role_prompt_context` (503), `_matching_effective_geography` (540), `_metric_direction` (552), `_percentage_comparison_policy` (564), `build_basket_context` (586), `optional_module_basket_relevance` (668) |
| `qa.py` | `QA_CORE_SECTIONS` (936), `QA_MODULE_SECTIONS` (942), `QA_SECTION_IDS` (948), `QA_MATERIAL_SEVERITIES` (949), `_normalized_qa_flags` (952), `_material_qa_flags` (976), `_correction_targets` (980), `_targeted` (1000), `_correction_flags_json` (1005), `qa_review_from_state` (1012), `normalize_qa_review` (1030) |
| `modules.py` | `CURRENCY_SYMBOLS` (82), `ReportModule` (1049), `ExchangeRateModule` (1085), `FuelEnergyModule` (1272), `LivestockAnimalProductsModule` (1385), `LabourMarketModule` (1496), `AVAILABLE_MODULES` (1617) |
| `nodes/data_agent.py` | `generate_mock_time_series` (1627), `calculate_statistics` (1674), `_select_default_commodities` (1709), `node_data_agent` (1732) |
| `nodes/graph_designer.py` | `_is_auxiliary_series` (307), `_categorize_commodity` (332), `_slugify` (355), `_chunk_list` (362), `_commodity_importance_score` (386), `_currency_axis_label` (739), `_fx_axis_label` (748), `_fuel_axis_label` (753), `_animal_axis_label` (758), `_labour_axis_label` (768), `_localized_category_name` (777), `_localized_page_suffix` (785), `_set_localized_numeric_axis` (792), `_set_localized_month_axis` (803), `_normalise_time_index` (817), `_index_to_first_observation` (826), `_history_overlay_values` (837), `_plot_history_overlays` (882), `_BASKET_ROLE_COLORS` (1935), `_basket_snapshot_for_chart` (1941), `_basket_series_frame` (1951), `_basket_chart_identity` (1969), `_localized_basket_scope` (1999), `_basket_trend_chart_data` (2007), `_basket_regional_target_data` (2070), `_legacy_primary_regional_target_data` (2112), `_encode_matplotlib_figure` (2137), `node_graph_designer` (2144) |
| `nodes/news_retrieval.py` | `node_news_retrieval` (2531) |
| `nodes/event_mapper.py` | `node_event_mapper` (2580) |
| `nodes/trend_analyst.py` | `_trend_with_basket_identity` (644), `node_trend_analyst` (2684) |
| `nodes/module_orchestrator.py` | `node_module_orchestrator` (2783) |
| `nodes/highlights_drafter.py` | `_has_currency_depreciation_driver` (912), `node_highlights_drafter` (2958) |
| `nodes/narrative_drafter.py` | `node_narrative_drafter` (3057) |
| `nodes/red_team.py` | `node_red_team` (3185) |
| `nodes/prepare_correction.py` | `node_prepare_correction` (3293) |
| `graph.py` | `OnStepCallback` (65), `MAX_CORRECTION_ATTEMPTS` (3290), `should_correct` (3303), `build_graph` (3313) |
| `service.py` | `run_report_generation` (3379) |
| each module that logs | `logger` (63), as `logging.getLogger(__name__)` |

- `prompt_registry.py` → `prompts.py` whole (`PROMPT_ROOT`, `MANIFEST_PATH`, `OUTPUT_FACING_PROMPTS`, `PromptRegistryError`, `_prompt_path`, `get_prompt_template`, `template_placeholders`, `render_prompt`, `_sha256`, `load_prompt_manifest`, `validate_prompt_manifest`, `assert_prompt_manifest_valid`).
- `_select_default_commodities` has no user (`data_loader.py:4200` has its own copy). It moves verbatim next to `node_data_agent` and is reported as a follow-up; it is not deleted here.
- A single-user helper lives with its node. The exceptions are the whole QA block (`qa.py`, above) and `_mapping`, which `basket_context.py` owns and `nodes/trend_analyst.py` imports.

### 5.2 MFI

| Today | After |
|---|---|
| `light_graph.State`, `build_graph` (with `stage()`) | `graph.py` |
| `light_graph.prepare_analysis` | `nodes/prepare_analysis.py` |
| `light_graph.retrieve_context`; `context.TOPIC_TERMS`, `context.retrieve_context_documents`, `context.logger` | `nodes/context_retrieval.py` (`context.py` removed). `retrieve_context` calls `retrieve_context_documents` in its own module instead of through a lazy import. |
| `light_graph.render_figures` | `nodes/charts.py` |
| `light_graph.texts`, closures `package_for`, `generate_family` | `sections.py`: `texts`, `package_for(state, family, ids, kind)`, `generate_family(runtime, ledger, state, family, kind)` |
| draft lambdas (`{n: generate_family(s, f, n)}`) | `nodes/draft.py`: `draft_family(runtime, ledger, s, f, n)` |
| review lambdas | `nodes/review.py`: `review_family(runtime, ledger, s, f, n)` |
| closure `correction` | `nodes/correct.py`: `correction(runtime, ledger, s, f, n)` |
| closure `synthesis` | `nodes/executive_summary.py`: `synthesis(runtime, s)` |
| closure `assemble` | `nodes/assemble_report.py`: `assemble(s)` |
| `light_contracts.POLICY`, `ANALYSIS_POLICY`, `STYLE_POLICY`, `RECOMMENDATION_POLICY`, `LIMITATION_POLICY`, `DIMENSION_GUIDANCE`, `MARKET_GUIDANCE`, `instructions` | `prompts.py` |
| the rest of `light_contracts` | `contracts.py` |
| `light_runtime`, `light_service`, `light_evidence`, `light_report` | `runtime`, `service`, `evidence`, `report` (whole modules) |

- Each closure body moves verbatim, with its free variables (`runtime`, `ledger`) as parameters. Its local names (`s`, `f`, `n`) are kept so the diff shows only the move.
- The graph still passes each per-family node its family and its own node name (`f=family, n=draft`, …), as the old lambdas did; the node modules never rebuild a node name.
- `runtime.py` imports `instructions` from `prompts` and the rest from `contracts`. `service.effective_contract` does the same. Its hashes are of the texts, so they do not change.

### 5.3 Seasonal Outlook

| Today | After |
|---|---|
| `engine.EVIDENCE_STAGES`, `CHAINS`, `initial_state`, `evidence_context` | `engine.py`, unchanged |
| `engine.request_for`, `accept`: code shared by the branches | `engine.py`: `evidence_inputs` (model case, aliases, `case_id` removed, `figure_ids` set), `evidence_images`, `stage_request` (the request dict), `parse_reply` (finish reason, empty reply, `json.loads`), `check_map_dates` (issue date after the cutoff, reversed validity) |
| each stage's branches of `request_for` and `accept` | `nodes/<stage>.py`: `request(state)`, `accept(state, response, namespace)` |
| name dispatch | `nodes/__init__.py`: `STAGES`, `request_for`, `accept` |
| the `export` lambda | `nodes/export.py`: `export(state, build)` |
| `science/evidence_prompts.py` | `prompts.py`: `COMMON`→`EVIDENCE_COMMON`, `EXTRACT`, `REVIEW`→`EVIDENCE_REVIEW`, `REFINE`, `FEEDBACK`, `prompt(state, stage)`→`evidence_prompt(state, stage)` |
| `science/report_prompts.py` | `prompts.py`: `ADAPTATIONS`, `effective_rules`, `COMMON`→`REPORT_COMMON`, `DRAFT`, `REVIEW`→`REPORT_REVIEW`, `REDRAFT`, `prompt(stage, state)`→`report_prompt(stage, state)` |

- The prompt texts, `prompt_version`, the contract versions and the stored `effective_rules` do not change. `evidence_prompt` still selects the refinement rules for the feedback stage.
- `science/profiles.py` (`RESOURCES = parents[1] / 'resources'`) and `inputs.py` do not move.

## 6. Tests, tools and paths

### 6.1 Monkeypatch targets

A patch works only on the module where the name is looked up at call time. There are no re-export shims in old modules: an old module that no longer has the name makes `monkeypatch.setattr` raise instead of silently patching nothing, and lint rejects unused imports. Each retargeted site is proven live with `patch_probe` (§8).

| Patched today | Sites | After |
|---|---|---|
| `market_graph._plot_history_overlays`, `_encode_matplotlib_figure` | 6 + 1 | `nodes.graph_designer` |
| `market_graph.resolve_report_price_data` | 1 | `nodes.data_agent` |
| `market_graph.llm_provider` | 5 | `runtime` (`llm_client` looks it up there) |
| `market_graph.build_graph` | 1 | `service` |
| `market_graph.SeeristRetriever`, `ReliefWebRetriever` | 1 + 1 | `nodes.news_retrieval` |
| `light_graph.render_figures` | 2 | `nodes.charts`. `test_mfi_light_workflow.py:118` has a local function named `charts`, so the test imports the module under an alias. |
| `light_graph.retrieve_context` | 1 | `nodes.context_retrieval` |
| `light_graph.build_graph` (must never be called) | 2 | `graph` (`service` imports it inside the function); proven with a watch (§8) |
| `context.SeeristRetriever`, `ReliefWebRetriever`, `retrieve_context_documents` | 4 + 4 + 1 | `nodes.context_retrieval` |
| `light_runtime.time.sleep` | 2 | `runtime.time.sleep` (the `time` module either way) |
| `light_contracts.<policy>` (not callable; the test's assertion proves it) | 3 | `prompts` |
| Seasonal `runner.profile`, `runner.llm_provider` | 2 | unchanged |

Import-only changes:
- `market_graph.<name>` → the module of each name (§5.1);
- `assert_prompt_manifest_valid` → `.prompts`;
- `light_contracts.NODES`, `response_schema`, `inspect_sections` → `contracts`;
- `instructions` → `prompts`;
- `light_runtime` / `light_service` → `runtime` / `service`;
- `engine.request_for`, `engine.accept` → `nodes.request_for`, `nodes.accept` (`engine.initial_state` stays);
- `test_seerist_retrieval` imports `mfi_drafter.nodes.context_retrieval` and `market_monitor.nodes.news_retrieval` (plus `state` for `create_initial_state`).

The page harnesses keep restoring `sys.modules` through `monkeypatch` (fixed in `ef83528`).

### 6.2 Code and tools that name modules as strings or paths

- `render_worker.py` stays in place: its subprocess runs `-m app.services.mfi_drafter.render_worker` with `cwd=parents[3]`.
- `__file__` paths:
  - `market_monitor/prompts.py` uses `with_name("prompt_templates")`;
  - `map_basemap.py`, `seasonal_outlook/inputs.py` and `science/profiles.py` do not move.
- The `.tmp` tools were made layout-tolerant in Phase 0 (§8.1).

### 6.3 Guard tests that read source files by path

Both are updated on purpose, keeping their intent:

- `tests/test_mfi_phase3.py` scans `light_graph.py` for legacy field reads. After the move, it scans MFI `graph.py`, `sections.py` and every `nodes/*.py`.
- `tests/test_llm_observability.py::test_report_workflows_have_no_direct_model_invocations` checks `light_graph.py` (only `runtime.invoke(` allowed) and MM `graph.py` (only `agent.invoke(`). After the move, it scans each drafter's `graph.py` and `nodes/*.py`, plus MFI `sections.py` and MM `service.py`, with the same allowed calls. Otherwise the model calls would sit in files the guard never reads.

## 7. Migration plan

Work happens on `refactor/drafter-layout`, based on `refactor/shared-layer` @ `68cec22`. One commit per step, each verified (§8). Each phase stops for the user's go-ahead.

| Phase | Commits |
|---|---|
| 0 | Branch, tolerant tools, new tools, baselines, this plan |
| 1 MFI | (1) the six `light_` renames with `git mv`, import rewiring and test import paths, nothing else; (2) `prompts.py` split from `contracts.py`; (3) `nodes/`, `sections.py`, slim `graph.py`, `context.py` merged into `nodes/context_retrieval.py` |
| 2 Seasonal | (1) `prompts.py` from the two science files; (2) `nodes/` per stage and `nodes/export.py`, `engine.py` reduced to the shared pieces. One offline browser cycle (extract → feedback → confirm → report) if the pages changed. |
| 3 MM | (1) `state.py`, `runtime.py`, `text.py`; (2) `prompts.py`: registry rename, `prompt_templates/` with `git mv`, the prompt inputs, the two builders; (3) `basket_context.py`, `qa.py`, `modules.py`; (4) nodes `data_agent`, `graph_designer`, `news_retrieval`; (5) `event_mapper`, `trend_analyst`, `module_orchestrator`; (6) `highlights_drafter`, `narrative_drafter`, `red_team`, `prepare_correction`; (7) slim `graph.py` and `service.py`, with `router.py`, `__init__.py` and `scripts/phase7_second_basket_qa.py` rewired |
| 4 Close-out | Docs; a structural guard test; the container check; Progress; final report |

- **MM order** (differs from the brief, which put `prompts.py` last): the modules and nodes import the prompt inputs. Creating `prompts.py` second moves each helper once, and no intermediate step has a `prompts ↔ graph` cycle. `__init__.py` is rewired in the same commit that moves each name it looks up.
- **Phase 4 docs:**
  - `docs/app-overview.md` (lines 59 and 155–161; tracked but ignored, so staged with `git add -f`);
  - `specs/mfi_light_workflow.md:29`, `specs/mfi_offline_map.md:48`, `specs/seasonal_outlook_implementation.md:19`;
  - the `README.md` layout if it lists modules;
  - `docs/second_food_basket.md`, which lists MM file paths as a feature record, only if it reads as a live reference.
  - Dated plans stay as records.
- **Guard test** `tests/test_drafter_layout.py`: each drafter has `graph.py`, `prompts.py` and a `nodes/` package, and no module under `nodes/` imports its drafter's `graph`. It must fail on `68cec22`.

## 8. Verification

### 8.1 Tooling (git-ignored, `.tmp/`)

- **Made layout-tolerant in Phase 0** (new import first, old as fallback; originals kept in `.tmp/drafter-layout/tools-orig/`):
  - `coherence-baseline/snapshot_mfi.py` gained `mfi_service()` and `mfi_retrieve_context()` and uses them (the latter also knows the layout between MFI's steps 1 and 3, where `retrieve_context` sat in `graph.py`);
  - `shared-layer/wire_mfi.py`, `p5_outputs.py` and `p5_offline_app.py` import through them;
  - `wire_mm.py` imports `service.run_report_generation`, falling back to `graph`;
  - `p5_outputs.py` also imports `nodes.news_retrieval` and `nodes.context_retrieval`;
  - `wire_seasonal.py` hooks `runner.Recorder.prepare(request)`, which every stage calls in both layouts, instead of `graph.request_for`.
- **New, in `.tmp/drafter-layout/`:**
  - `capture.sh NAME [mm mfi seasonal p5 preview pages]`: wire captures, MFI and Seasonal snapshots, the `p5_outputs` capture, the pages' report preview, `import_all`, `smoke_pages`.
  - `compare.sh BASE NAME [parts]`: `compare_wire`, `compare_snapshots`, `compare_seasonal`, `compare_stored.py` (what the Seasonal record stores, with only the random suffix of call ids masked), `p5_outputs compare`, and a diff of the preview captures.
  - `.tmp/shared-layer/p5_page_blocks.py --capture OUT`: the report preview of the 11 Phase 5 fixtures through `streamlit_shared.render_report_blocks` and the drafters' `ui.py` of the working tree, recorded as Streamlit calls. Its old `OLD_REV` mode compares against a revision that rendered without the drafter tables (before Phase 5 of the shared-layer refactor), so it always reports differences against `68cec22`; the capture mode replaces it here.
  - `moved_code.py [REV]`:
    - For every top-level definition of the old modules at `REV`, it finds the same name (or its agreed new name) in the drafter package and compares normalised ASTs. Relative imports are resolved, and old module paths are mapped to their new homes.
    - It reports identical, changed (with a diff), missing, duplicated, and new names.
    - It proves MM moves verbatim, and shows exactly what changed where MFI closures and Seasonal branches became functions.
  - `patch_probe.py` (pytest plugin) and `run_probes.py LIST`:
    - `PROBE=module:name` replaces what a test patches there with a callable that raises and counts. Hit > 0 proves the patch is live.
    - `PROBE_WATCH=module:name` counts calls through the real name. It checks the patches that must never be called.
    - One pytest run per target, so one probe's exception cannot hide another's.
  - `stage_diff.py [REV]` (Seasonal): loads the Seasonal package of `REV` under another package name and walks the seven stages for AFY, AMX and ASE, comparing, stage by stage, `REV`'s `engine.request_for`/`accept` with the working tree's:
    - the requests, key order included;
    - the outcome of the fake model's reply and of variants that reach every check of `accept`: blocked, incomplete, empty and invalid replies, missing fields, maps issued after the cutoff or valid over a reversed interval (on one map and on two, which fixes the order of the checks), unknown figures, maps without signals, reviews of unknown or missing maps, a review that raises an issue and the refinement decisions on it (none, one, twice, with a late map), non-verbatim feedback quotes, refused reports, unknown evidence and seasons, every season of the calendar, and both report validators forced to fail;
    - an outcome is the resulting state, or the exception type and message.
    A deliberate swap of the two map-date checks in a restored copy of `engine.py` made it report 12 differences.
  - `mm_move.py STEP` (Market Monitor): moves the definitions §5.1 assigns to a step's modules out of `graph.py`.
    - Each definition is cut verbatim, with a comment attached directly above it; section banners left without a definition go.
    - Each module's imports are derived from its code: `symtable`'s global names plus the names in annotations. They are rendered in `graph.py`'s groups, with one statement per module.
    - The tests' `market_graph.<name>` references and `setattr(market_graph, "<name>")` patch targets are pointed at each moved name's module.
    - A definition that would need a name still in `graph.py` stops the step. So would any module other than `service.py` importing `graph.py`.
    - It does not see patches of names `graph.py` only imported: `resolve_report_price_data` in step 4, `build_graph` in step 7. Those were retargeted by hand, and the probes check them.
  - `closure_bodies.py [REV]` (MFI step 3): compares the body of each closure of the old `build_graph` with the function that replaced it, adding the `runtime` and `ledger` arguments to the `generate_family` calls the closures made.

### 8.2 Per commit

- Full suite in the background, compared with `compare_junit.py`. Base: 912 tests, 909 passed, 3 skipped. No status may change, and the test count stays constant.
- `import_all.py`, `lint.py` on the touched files, `smoke_pages.py`.
- `moved_code.py 68cec22`: every definition moved verbatim or changed only as §5 describes.
- `run_probes.py` on every retargeted patch site.
- The touched drafter's wire capture: requests identical. Its snapshot: identical, including MFI's `effective_contract`.
- MM and MFI: the `p5_outputs` capture, identical.
- MM: the preview capture, identical.
- MM's prompts step: `git diff --stat -M` shows the 22 template and manifest files as 100% renames, and `test_prompt_manifest_is_complete_and_synced` passes.
- **Git:**
  - stage explicit paths only, and check `git status --short` before and after;
  - write commit messages to a file and commit with `git commit -F`;
  - no push, no PR, no Docker restart.

### 8.3 Per phase

- All captures of all three drafters: a move in one drafter must not change another.
- Phase 4: the container check (`p6_container.sh`, built from an LF archive of `HEAD`).

### Progress

**Phase 0, done 2026-09-26** on `refactor/drafter-layout` (from `68cec22`; this plan is its only commit):
- **Move map** (§5), checked by script: every one of the 90 top-level names of MM `graph.py` has exactly one home; an independent review of the map found no import cycle and folded four traps into §4 (MFI loop locals shadowing the node modules, LangGraph's injected `runtime` parameter, a test's local `charts` name, Seasonal payload and check order).
- **Tools** (§8.1): six tools made layout-tolerant, the preview gained a capture mode, and `capture.sh`, `compare.sh`, `compare_stored.py`, `moved_code.py`, `patch_probe.py` and `run_probes.py` were added.
- **Baselines** in `.tmp/drafter-layout/base/`, all on `68cec22`:
  - suite: 912 tests, 909 passed, 3 skipped, identical to `junit-p6-docs.xml`;
  - MM wire: 12 requests (English and French; TradingEconomics reachable), identical to `p5-final-wire-mm`;
  - MFI wire: 14 generation and 14 token-count calls, identical to the Phase 2 capture;
  - Seasonal wire: 21 requests over AFY, AMX and ASE, identical to the Phase 2 capture; the stored records too, with call-id suffixes masked;
  - MFI snapshots (Benin, Haiti, Gaza, misc) identical to `mfi-p5-final`; Seasonal snapshots identical to `seasonal-p5-diag`;
  - `p5_outputs`: 81 files, identical to `p5-final`;
  - preview: 11 fixtures, identical on a second capture;
  - 111 modules import; every page renders (the Seasonal page shows its expected "storage not configured").
- **Tool checks.**
  - Each new capture equals the last capture of the old tool, so the tool edits changed nothing they record. That includes the Seasonal wire hook moving from `graph.request_for` to `Recorder.prepare`.
  - `moved_code.py 68cec22` on the unchanged tree: every definition identical. The only names listed missing are the six Seasonal names still to be renamed.
  - `run_probes.py probes-base.txt`: all 16 patch sites are live at their current targets, and the two `build_graph` patches that must never be called are checked by a watch (called 1).
  - Probing all targets in one run reports false misses: the first probe raised stops the code before it reaches the next, for example `ReliefWebRetriever` before `SeeristRetriever`. So each target runs alone.

## 9. Decisions

Taken on 26 September 2026:

1. **One file per node type**, not per node name (MFI draft/review/correct serve both families; Seasonal one per stage plus `export`; MM one per node).
2. **Node files in a `nodes/` subpackage** of each drafter; `graph.py` at the drafter's root.
3. **One `prompts.py` per drafter**; prompt bytes unchanged; the MM templates stay `.txt` files with their manifest.
4. **MFI drops the `light_` prefix** from its module names; stored identifiers (`mfi-light-v1`, `light_phases`, `mfi.light.*.v1`, …) stay.
5. **Logger names follow the modules** (§2).
6. **MM `run_report_generation` moves to `service.py`.**
7. **The MM chart helpers stay with their only user**, `nodes/graph_designer.py`.

## 10. Risks

- **Silent stale patches** (a test patches a name the code no longer looks up there). Mitigations: no shims, the lint check for unused imports, and `run_probes.py` on every retargeted site.
- **Circular imports.** Nodes never import `graph`, the import direction is fixed (§4.3), and `import_all` runs on every commit.
- **Line endings.**
  - The MM templates are LF in the index and on disk, and their hashes are of the LF bytes. They move only with `git mv` and are never rewritten by a script.
  - The container is built from `git -c core.autocrlf=false archive`.
- **Network-dependent MM wire count:** 12 requests when TradingEconomics answers, 10 when it doesn't. Compare like with like.
- **A missed payload or check order** in the Seasonal split would change a prompt byte or a recorded error. The wire and snapshot captures cover all seven stages in three regions.

**Phase 1 (MFI), done 2026-09-26**, three commits, each verified as §8.2 describes:
- **`fed7ae9` — the six `light_` renames.** `git mv` of `light_graph`, `light_service`, `light_runtime`, `light_contracts`, `light_evidence` and `light_report`; git records all six as renames. Otherwise 14 import lines in the app (the package's lazy exports, the router, the modules' imports of each other) and the tests' import paths: the tests hold the modules as `mfi_graph`, `mfi_service`, `mfi_runtime` and `mfi_contracts`, because `runtime`, `service` and `graph` are local names in them. `moved_code.py`: the 77 MFI definitions identical.
- **`3204eda` — `prompts.py`.** The seven policy and guidance texts and `instructions` move from `contracts.py` to `prompts.py` (the split was cut at the first text and checked at both ends by script); `runtime.py` and `service.effective_contract` import `instructions` from there. `moved_code.py`: all eight identical; `effective_contract` changed only in its function-local import. The policy-change test patches `prompts`, and its assertions (every prompt hash changes) prove the patch is live.
- **`030d839` — `nodes/` and `sections.py`.**
  - `context.py` moved with `git mv` to `nodes/context_retrieval.py` and gained `retrieve_context`, minus its two function-local imports of names now in the same module. Git pairs the two paths as a rename at 48% similarity, just under its 50% default: `git log --follow -M40%` follows the history.
  - `closure_bodies.py`: the bodies of `package_for`, `generate_family`, `correction`, `synthesis` and `assemble` are identical to the closures they replace.
  - `build_graph` adds its nodes and edges in the same order as before.
  - The node modules are imported under aliases. LangGraph never sees a parameter named `runtime`, because every node is still registered through `stage()`.
  - `import_all`: 121 modules.
- **Tests:** only import paths and patch targets changed, plus the source paths of the two guard tests (§6.3). After each commit: 912 tests, 909 passed, 3 skipped, no status changed.
- **Probes after `030d839`:** all 7 retargeted MFI patch sites are live, with the same install and hit counts as at the base: `nodes.charts.render_figures` 19/15, `nodes.context_retrieval.retrieve_context` 18/15, the two retrievers 4/4 each, `retrieve_context_documents` 1/1, `time.sleep` 36/15, and the `graph.build_graph` watch called 1.
- **Captures after each commit, identical to the base:** MFI wire (14 generation and 14 token-count calls), MFI snapshots including `effective_contract` (Benin, Haiti, Gaza, misc), `p5_outputs` (81 files), the preview (11 fixtures), `import_all`, the pages.
- **Other drafters:** at the end of the phase, the MM wire (12 requests) and the Seasonal wire (21 requests, stored records) and snapshots are identical to the base.
- **Tooling fix during the phase:** `snapshot_mfi.py`'s context fallback knew only the old and the final layouts. Step 1's first capture failed on its `misc` part, and passed once the intermediate layout was added (§8.1).

**Phase 2 (Seasonal Outlook), done 2026-09-27**, two commits, each verified as §8.2 describes:
- **`e2ca35a` — `prompts.py`.**
  - `science/evidence_prompts.py` (`git mv`) and `science/report_prompts.py` were merged by script, with the texts copied, not retyped.
  - The clashing names were renamed, each occurrence counted: `EVIDENCE_COMMON`/`REPORT_COMMON`, `EVIDENCE_REVIEW`/`REPORT_REVIEW`, `evidence_prompt`/`report_prompt`.
  - The two files' docstrings are kept as section comments.
  - `moved_code.py`: all 13 texts and functions identical under their new names. `engine.request_for` changed only in its two prompt calls.
- **`5a0d469` — `nodes/`.**
  - Seven stage modules with `request` and `accept`, a dispatcher in `nodes/__init__.py`, and `nodes/export.py`.
  - `engine.py` now holds `EVIDENCE_STAGES`, `CHAINS`, `initial_state`, `evidence_context`, and the five shared pieces (§5.3). It no longer imports the report contract.
  - `build_graph` changed only in its export node and its imports.
  - `stage_diff.py 68cec22`: 448 requests and reply outcomes compared, 0 differences. Every outcome is the same as on the step 1 tree.
- **Tests:** only the module of the dispatch calls changed, `engine` to `nodes` (ten calls and one docstring). After each commit: 912 tests, 909 passed, 3 skipped, no status changed. The two Seasonal probes are live after each commit: `runner.profile` 35/20, `runner.llm_provider` 1/1.
- **Captures after each commit, identical to the base:** the Seasonal wire (21 requests over AFY, AMX and ASE, all seven stages, export included), the stored records (with call-id suffixes masked), the snapshots, `import_all` (129 modules) and the pages.
- **Browser cycle:** not run. No page or `ui.py` changed, the condition the brief sets for it, and the wire and snapshot flows run extract → feedback → confirm → report → export through `run_phase` end to end.
- **Other drafters:** at the end of the phase, the MM wire (12 requests), the MFI wire (28 calls), the MFI snapshots, `p5_outputs` (81 files) and the preview (11 fixtures) are identical to the base.

**Phase 3 (Market Monitor), done 2026-09-27**, seven commits, each verified as §8.2 describes (`moved_code.py`, lint, `import_all`, the suite, the MM wire, `p5_outputs`, the preview, the pages and the probes):
- **`0e1d019` — `state.py`, `runtime.py`, `text.py`.** The five `llm_provider` patches target `runtime`, where `llm_client` looks the name up. The package's lazy `create_initial_state` reads `state`. `get_type_hints(MarketReportState)` resolves all 50 fields from `state.py`, as LangGraph needs.
- **`8a7f517` — `prompts.py`.**
  - `git mv` of `prompts/` to `prompt_templates/` (22 files) and of `prompt_registry.py` to `prompts.py`: 23 renames at 100% similarity. The templates are still LF in index and working tree, and the manifest test passes.
  - The prompt inputs were copied from `graph.py`'s text.
  - The two builders hold the nodes' f-strings: a check asserted that each builder contains the node's literal, with `{state['country']}` → `{country}` as the only edit.
  - The MM wire (12 requests, event extraction and trend analysis in English and French) is identical.
- **`34dbbb4` — `basket_context.py`, `qa.py`, `modules.py`.** The router imports `AVAILABLE_MODULES` and `normalize_qa_review` from their modules, and the lazy exports read `modules`. Four section banners left empty were dropped.
- **`717cfd8` — nodes `data_agent`, `graph_designer` (873 lines with its 27 helpers), `news_retrieval`.** The Phase 7 QA script imports `node_graph_designer` from its node module, and `test_market_monitor_phase7` passes.
- **`4ddd79a` — nodes `event_mapper`, `trend_analyst`, `module_orchestrator`.**
- **`448bdba` — nodes `highlights_drafter`, `narrative_drafter`, `red_team`, `prepare_correction`.** Also removes three duplicate node imports that `4ddd79a` left in `graph.py` (see below).
- **`d9ba22a` — `service.py` and the slim `graph.py` (109 lines).** The router and the lazy `run_report_generation` export read `service`. The test replaces `build_graph` in `service`. The guard against direct model calls reads `graph.py`, `service.py` and every node module.
- **Evidence.**
  - `moved_code.py 68cec22`: all 90 names of `graph.py` and the 12 of `prompt_registry.py` are identical in their new modules. The exceptions are the three intended changes (`PROMPT_ROOT`, and the two nodes calling the builders) and the two new builders.
  - After each commit: 912 tests, 909 passed, 3 skipped, no status changed. MM wire: 12 requests, 0 differences. `p5_outputs`: 81 files, 0 differences. Preview: 11 fixtures identical. Pages: all render.
  - The 8 MM patch sites are live at their final targets. That includes the router's own `run_report_generation` patch, whose target did not change.
- **What went wrong and was caught.**
  - The mover twice produced wrong imports on its first run of a step: duplicated names in step 2, and a node module path in step 4, where it stopped before writing. Each time its output was discarded, the mover fixed, and the step rerun from the committed tree.
  - It also rendered a one-name statement on three lines. That was fixed before step 4 was committed.
  - One slip reached a commit: `4ddd79a`'s `graph.py` imports three node functions twice. It is harmless, since the same objects are imported, but untidy. The mover then merged statements of the same module, `448bdba` removes the duplicates, and a scan finds no repeated import in any MM module.
  - In step 4, a test failed because its `resolve_report_price_data` patch still targeted `graph.py`. It failed loudly, since no shim was left, and the probe pinpointed the target. It was retargeted before the commit.
- **Phase-end check after `d9ba22a`:** every capture of all three drafters is identical to the base. That is MM, MFI (wire and snapshots), Seasonal (wire, stored records and snapshots), `p5_outputs`, the preview and the pages, with `stage_diff.py` at 448 comparisons and 0 differences.
