# Second Food Basket — Detailed Implementation Plan

Status: proposed implementation plan; no application code has been changed.

## 1. Objective

Extend the Price Bulletin Drafter from one compulsory food basket to:

- one compulsory **primary basket**;
- one optional, saved **secondary basket**;
- a per-report option to exclude the saved secondary basket without deleting it.

The primary basket defaults to the name **MEB**. The secondary basket has no default name and requires a Country Office-provided name and description.

The change must preserve the existing single-basket workflow, calculations, outputs, and API compatibility wherever possible. It must also preserve basket identity and scope throughout data loading, calculations, charts, narratives, QA, and DOCX export.

## 2. Confirmed Product Decisions

### 2.1 Primary basket

- The primary basket is compulsory for report generation.
- Its default name is `MEB`.
- The UI should explain that MEB means Minimum Expenditure Basket.
- The Country Office can replace the default name.
- If the name remains `MEB`, a user-authored description is optional.
- If the name is changed from `MEB`, a short description is mandatory.
- A neutral automatic description should be stored when the default is used, for example:
  - `Primary MEB reference basket configured by the Country Office.`
- Composition and positive quantities remain mandatory.
- The basket defaults to national applicability.
- If it is not national, at least one available region must be selected.
- The primary basket cannot be removed.

### 2.2 Secondary basket

- The secondary basket is optional.
- It is created using an explicit `Add second basket` action.
- Its name is mandatory and has no automatic default.
- Its short description is mandatory.
- Description guidance should include examples such as:
  - affordability proxy;
  - WFP emergency ration;
  - healthy-diet proxy;
  - urban basket;
  - pastoral basket.
- Composition and positive quantities are mandatory.
- It defaults to national applicability.
- If it is not national, at least one available region must be selected.
- Once saved, it remains saved for subsequent report iterations.
- It may be edited, archived/removed, or temporarily excluded from a report.

### 2.3 Per-report inclusion behaviour

- When a saved secondary basket exists, `Include second basket in this report` defaults to **on**.
- A user may turn it off for the current report without deleting or modifying the saved basket.
- Turning it off is a run-level choice and must not be persisted as the default for the next iteration.
- On the next new report iteration, the option defaults to on again.
- If the secondary basket is archived/removed, the inclusion control is not shown.
- A completed run records whether the secondary basket was included and which immutable basket versions were used.

### 2.4 Geographic scope

- Each basket independently has either:
  - `national` scope; or
  - `selected_regions` scope.
- The secondary basket's configured scope does not cause the primary basket to be recalculated for the same regions.
- A national primary basket and a regional secondary basket remain distinct measures.
- The report must not present a regional secondary value as national.
- The narrative must not make direct absolute-cost comparisons when basket scopes differ.

### 2.5 Removal and history

- Removing the secondary basket should archive it rather than physically delete historical rows.
- Archived basket versions remain available to old report snapshots and audit history.
- Archiving removes the active secondary pointer so it is no longer offered in new reports.
- Re-adding a secondary basket creates a new published version in the secondary role.
- Primary-basket removal must be rejected by both UI and backend validation.

## 3. Scope and Non-Goals

### In scope

- Exactly two basket roles: `primary` and `secondary`.
- One active version per country and role.
- Names, descriptions, geographic scope, compositions, and quantities.
- Persistent secondary basket with default-on, per-run exclusion.
- Separate calculations, statistics, coverage, charts, narrative facts, and QA.
- English, French, and Spanish report output.
- Streamlit, API, asynchronous generation, JSON result, and DOCX export.
- Backward compatibility for clients that only use the current primary basket.

### Not in scope

- An unrestricted library of three or more concurrently reportable baskets.
- Recalculating the primary basket over the secondary basket's regions.
- Automatically comparing national and regional basket cost levels.
- Automatically converting basket scopes or denominators.
- User-specific private baskets; the existing Country Office/shared configuration model remains.
- Deleting historical basket versions used by previous reports.

## 4. Terminology and Stable Internal Roles

User-facing names are variable, but internal roles must be stable:

| Internal role | Requirement | Default name | Can be removed | Can be excluded per run |
|---|---|---|---|---|
| `primary` | compulsory | `MEB` | no | no |
| `secondary` | optional | none | yes, by archive | yes |

Prompts, statistics, APIs, and charts must use stable role keys and carry the user-facing name alongside them. The system must never use a user-entered name as a database key or dataframe identifier.

## 5. Target Data Model

The current model supports one country-level active basket. It should be extended to support one active version per `(country_iso3, basket_role)`.

### 5.1 `country_food_basket_versions`

Add fields:

```text
basket_role             text/enum: primary | secondary
basket_name             text, required
short_description       text, nullable only for a default primary MEB
scope_type              text/enum: national | selected_regions
```

Existing fields remain, including version ID, country, status, creator, timestamps, cache version, and change note.

Validation rules:

- `basket_role` must be `primary` or `secondary`.
- `basket_name` must be trimmed and non-empty.
- Secondary description must be trimmed and non-empty.
- A custom-named primary description must be trimmed and non-empty.
- National versions must have no configured region rows.
- Regional versions must have at least one configured region row.
- At most one version per country and role may be active through the current-pointer table.

Version numbers may remain country-wide for compatibility, although role-specific numbering would be clearer in the UI. If country-wide numbering is retained, the UI must display both role and version number.

### 5.2 `country_food_basket_regions`

Create a normalized scope table:

```text
basket_region_id        uuid/text primary key
basket_version_id       foreign key to country_food_basket_versions
region_name             text
sort_order              integer
unique(basket_version_id, region_name)
```

Region names should be validated against the active PriceCache country metadata at save time. Store the canonical region-name snapshot because current country metadata exposes region names rather than a guaranteed stable region ID.

### 5.3 `country_food_basket_current`

Change the current pointer from one row per country to one row per country and role:

```text
country_iso3
basket_role             primary | secondary
active_basket_version_id
updated_at
updated_by_user_id
primary key(country_iso3, basket_role)
```

This permits a primary and secondary version to remain active concurrently. Publishing a new primary supersedes only the previous primary; publishing a new secondary supersedes only the previous secondary.

### 5.4 Migration strategy

Create matching SQLite and PostgreSQL migrations, expected as the next migration after `004_country_food_baskets.sql`.

Migration behaviour for existing data:

1. Add/backfill `basket_role = 'primary'`.
2. Add/backfill `basket_name = 'MEB'`.
3. Add/backfill the neutral primary description.
4. Add/backfill `scope_type = 'national'`.
5. Rebuild or replace `country_food_basket_current` with the composite primary key.
6. Preserve all existing basket version and item IDs.
7. Point every existing country current row to the corresponding primary version.
8. Add the region-scope table.
9. Add role/current-pointer indexes and constraints.

SQLite will likely require table reconstruction to change the primary key. The migration must run transactionally and be verified against a database containing pre-migration basket data.

## 6. Basket Domain and Repository Changes

Primary files:

- `app/services/market_monitor/food_basket.py`
- `app/services/price_cache/migrations/sqlite/`
- `app/services/price_cache/migrations/postgres/`

### 6.1 Domain objects

Extend basket save input and stored basket objects with:

```text
basket_role
basket_name
short_description
scope_type
regions
```

Introduce stable constants or an enum for role and scope values.

### 6.2 Repository interface

Replace role-ambiguous methods with role-aware operations:

```text
get_active_basket(country_iso3, role='primary')
get_active_baskets(country_iso3)
get_basket_version(country_iso3, basket_version_id)
list_basket_history(country_iso3, role, limit)
save_basket(country_iso3, role, metadata, regions, items, ...)
archive_secondary_basket(country_iso3, ...)
```

Compatibility wrappers may preserve the current primary-only function names temporarily.

### 6.3 Save and archive rules

- Saving a basket supersedes only the active version for the same role.
- Saving a primary with an empty name converts the name to `MEB`.
- Saving a custom primary without a description is rejected.
- Saving a secondary without a name or description is rejected.
- Saving `selected_regions` without valid regions is rejected.
- Saving `national` with region selections either clears them explicitly or rejects inconsistent input; clearing is preferable in the UI, while the backend should normalize to no rows.
- Archiving the primary is rejected.
- Archiving the secondary changes its active version to `archived` and removes the secondary current pointer.

### 6.4 Report snapshot resolution

Create a resolver that returns:

```text
primary: required active immutable version
secondary: active immutable version or null
secondary_included: run-level boolean
```

If the run includes the secondary basket, both submitted version IDs must still be current when generation starts. A stale primary or included secondary returns a version conflict with the affected role and name.

## 7. API and Dispatcher Changes

Primary files:

- `app/services/market_monitor/schemas.py`
- `app/services/market_monitor/router.py`
- `app/streamlit_backend/dispatcher.py`

### 7.1 Basket configuration endpoints

Recommended endpoints:

```text
GET    /market-monitor/countries/{country}/baskets
POST   /market-monitor/countries/{country}/baskets/primary
POST   /market-monitor/countries/{country}/baskets/secondary
GET    /market-monitor/countries/{country}/baskets/{role}/history
DELETE /market-monitor/countries/{country}/baskets/secondary
```

Expected GET response:

```json
{
  "country": "South Sudan",
  "iso3": "SSD",
  "primary": { "basket_version_id": "...", "basket_name": "MEB", "items": [] },
  "secondary": { "basket_version_id": "...", "basket_name": "Pastoral Basket", "items": [] },
  "needs_primary_setup": false,
  "has_secondary": true
}
```

Keep `GET/POST /countries/{country}/basket` as a temporary primary-basket compatibility alias. Document it as primary-only.

Dispatcher routes must mirror router behaviour because the Streamlit application can use the in-process backend path.

### 7.2 Report generation input

Extend `GenerateReportInput` with:

```text
primary_basket_version_id: optional string
include_secondary_basket: boolean = true
secondary_basket_version_id: optional string
```

Compatibility behaviour:

- Existing `basket_version_id` maps to `primary_basket_version_id`.
- A missing primary version ID may resolve to the current primary for legacy callers, though the UI should always submit the selected immutable version.
- `include_secondary_basket=true` includes the current secondary when one exists.
- If no secondary exists, `include_secondary_basket=true` is normalized to false without error.
- If a secondary exists and inclusion is false, its version ID is ignored and it does not affect data requirements or reportable months.

### 7.3 Report generation output

Add:

```text
food_baskets:
  primary: basket snapshot
  secondary: basket snapshot or null

basket_statistics:
  primary: statistics
  secondary: statistics or null

secondary_basket_included: boolean
```

Retain the current top-level `food_basket` and `data_statistics.food_basket` as primary-basket compatibility aliases during migration.

### 7.4 Reportable months and refresh

Reportable-month requests must include the selected basket versions or inclusion state. Recommended payload/query contract:

```json
{
  "primary_basket_version_id": "...",
  "include_secondary_basket": true,
  "secondary_basket_version_id": "..."
}
```

The response should expose:

```text
primary_reportable_months
secondary_reportable_months
joint_reportable_months
latest_primary_reportable_month
latest_secondary_reportable_month
latest_joint_reportable_month
missing_by_basket_and_month
```

Manual DataBridges refresh must request the union of required commodity IDs for the baskets included in the run and report unresolved gaps by basket role/name.

## 8. Streamlit UI Plan

Primary file:

- `pages/3_Price_Bulletin_Drafter.py`

### 8.1 Refactor the basket editor

Extract reusable rendering/data-building helpers so primary and secondary editors use the same composition and quantity validation rather than duplicating the current large inline block.

Suggested UI order:

1. Country selection and cache metadata.
2. `Primary basket` card.
3. `Second basket` card or `Add second basket` action.
4. Run-level second-basket inclusion control.
5. Reportable period and remaining report parameters.

### 8.2 Primary basket card

Show:

- required badge;
- name input prefilled with `MEB`;
- help text `Minimum Expenditure Basket`;
- description input;
- scope radio: national / selected regions;
- conditional region multiselect;
- composition and quantity editor;
- version, creator, cache version, updated date, and change note;
- save/publish action.

Validation:

- primary name cannot be empty after defaulting;
- description required when normalized name is not `MEB`;
- regional scope requires at least one region;
- at least one positive-quantity component is required.

### 8.3 Secondary basket card

When absent:

- show `Add second basket`;
- opening it reveals an unsaved editor;
- cancelling leaves no secondary configuration.

When present, show:

- optional badge;
- saved name and description;
- scope and selected regions;
- composition and quantities;
- version/audit metadata;
- edit/publish action;
- `Remove second basket` with confirmation.

Validation:

- name required;
- description required;
- regional scope requires at least one region;
- at least one positive-quantity component required.

### 8.4 Per-run inclusion control

When a secondary exists, show outside the main Streamlit form:

```text
[x] Include <secondary basket name> in this report
```

It should be outside the form because changing it affects reportable-month options and may need an immediate rerun.

State behaviour:

- initialize to true for a newly selected country;
- reset to true when the active secondary version changes;
- reset to true after starting/completing a report iteration;
- do not save a false value to the basket configuration;
- keep false only long enough to submit the current intended run.

Define an explicit report-iteration/session-state reset rather than relying on `st.checkbox(value=True)`, because Streamlit widget state otherwise preserves the unchecked value across reruns.

### 8.5 Region interaction

- Basket scope regions come from current country metadata.
- Report regions and basket scope are different concepts.
- Effective regional basket calculation uses the intersection of configured basket regions and regions selected for the run.
- If an included regional basket has no overlap with run regions, block submission with an actionable message or invite the user to uncheck the secondary basket.
- Do not silently broaden the configured basket scope.
- Do not recalculate the primary over the secondary scope.

### 8.6 Commodity selection

- Primary components remain locked into every run.
- Included secondary components are also locked into that run.
- The `Additional commodities` list excludes the union of included basket commodities.
- If the secondary is unchecked, its components do not become mandatory and may appear as optional additional commodities.
- Internally resolve the union by commodity ID, not commodity name.

## 9. Data Loading and Calculation Plan

Primary file:

- `app/services/market_monitor/data_loader.py`

### 9.1 Basket calculation specification

Introduce an internal `BasketCalculationSpec` per role containing:

```text
role
basket_version_id
name
description
scope_type
configured_regions
items with commodity IDs, quantities, and units
```

Build a list containing the primary and, when included, the secondary.

### 9.2 Price retrieval

- Query the union of commodity IDs required by included baskets and additional commodities.
- Preserve the mapping from each commodity to each basket; the same commodity may belong to both baskets with different quantities.
- Never flatten basket items into one de-duplicated component list for calculation.
- Existing targeted backfill should fetch a commodity once but apply the retrieved price independently to each basket that uses it.

### 9.3 Canonical basket series

Keep the commodity price dataframe unchanged and introduce canonical long-form basket series:

```text
Date
BasketRole
BasketVersionId
BasketName
ScopeType
ScopeLabel
Region (nullable for national series)
Cost
SelectedComponentCount
AvailableComponentCount
MissingComponentNames
Complete
```

Produce:

- `basket_series_national` for nationally applicable baskets;
- `basket_series_regional` for region-level results and regional-scope baskets.

For backward compatibility, continue populating the existing `FoodBasket` national/regional field from the primary basket only. Optional modules or diagnostics that still consume `FoodBasket` must therefore remain tied to the primary role.

### 9.4 Scope calculation

National basket:

- calculate from the existing national monthly commodity averages;
- optionally calculate regional breakdowns for the existing regional chart.

Regional-scope basket:

- calculate only for configured regions included in the run;
- do not generate or label a national value;
- if a combined scope average is needed for a summary, label it explicitly as the average over the configured/included regions rather than national;
- retain individual region values for regional reporting.

The primary is not recalculated to match the secondary scope.

### 9.5 Missing data policy

For each basket, month, and applicable region:

- complete means every configured component has a valid price;
- incomplete results must not be silently treated as full basket costs;
- target-month incompleteness blocks use of that basket in the report;
- an included incomplete secondary blocks the joint report and instructs the user to obtain data or uncheck the secondary;
- if the secondary is unchecked, only primary completeness gates the report;
- MoM is null unless the current and previous month are both complete for that basket/scope;
- YoY is null unless the current and year-ago month are both complete;
- regional bars/claims exclude incomplete region/basket combinations and surface warnings.

This removes the current risk of `sum(skipna=True)` producing a partial basket that appears complete.

### 9.6 Reportable-month logic

- Primary reportable months are compulsory.
- When the secondary is included, available report periods are the intersection of primary and secondary reportable months.
- When it is unchecked, report periods immediately revert to primary reportable months.
- Cache reportability by cache version plus both selected basket version IDs and inclusion state.
- The UI must explain when adding the secondary moves the latest reportable month backwards.

### 9.7 Statistics

Calculate per-role statistics:

```text
current_cost
mom_change_pct
yoy_change_pct
current_complete
mom_reference_complete
yoy_reference_complete
selected_component_count
available_component_count
missing_component_names
applicable_regions
scope_label
component_contributions
```

Component contributions should be deterministic and may include absolute contribution and share of complete basket cost. They provide grounded evidence for basket-specific narrative drivers.

### 9.8 Optional-module dependencies

- FX price-scale diagnostics should continue using the primary basket only unless deliberately redesigned.
- Labour purchasing-power staple selection should explicitly use primary-basket components only.
- Secondary components must not change the selected labour staple through list ordering.
- Fuel, exchange-rate, livestock, and labour narratives may receive basket metadata, but must not invent causal relationships.

## 10. Graph State and Workflow Changes

Primary file:

- `app/services/market_monitor/graph.py`

Extend state with:

```text
primary_basket_version_id
include_secondary_basket
secondary_basket_version_id
food_baskets
basket_series_national
basket_series_regional
basket_statistics
```

Retain current singular fields as primary aliases during the compatibility period.

### 10.1 Data agent

- Resolve immutable primary and optional secondary snapshots.
- Load price data once for the union of requirements.
- Calculate each basket independently.
- Emit basket-specific warnings using name and role.
- Store full basket snapshots in run metadata.

### 10.2 Graph designer

Primary basket:

- preserve the existing primary trend and regional chart behaviour.

Included national secondary:

- show both series in one chart only when they share compatible scope and units;
- otherwise show clearly labelled small multiples;
- retain stable colours by role, e.g. primary blue and secondary orange.

Included regional secondary:

- keep the primary chart unchanged;
- create a separate secondary chart limited to configured/included regions;
- name and caption it with the basket name and scope;
- do not put a regional secondary line on a national primary cost axis as though they are equivalent.

Suggested stable figure IDs:

```text
food_basket_trend_primary
food_basket_trend_secondary
food_basket_trend_combined
regional_comparison_primary
regional_comparison_secondary
```

Maintain `food_basket_trend` and `regional_comparison` as primary aliases until report assembly and clients are migrated.

### 10.3 Trend analyst

Pass ordered basket statistics and metadata. Require the internal analysis to:

- analyze primary first;
- analyze secondary separately when included;
- use user-provided descriptions to interpret purpose;
- identify scope before characterizing a value;
- compare percentage direction only when meaningful;
- avoid absolute comparison when scope differs;
- never merge or average basket values;
- ground basket drivers in component contributions.

## 11. Prompting Plan

Primary files:

- `app/services/market_monitor/prompts/en/`
- `app/services/market_monitor/prompts/fr/`
- `app/services/market_monitor/prompts/es/`
- `app/services/market_monitor/prompts/manifest.json`
- embedded trend prompt in `app/services/market_monitor/graph.py`

Create one deterministic prompt object, for example `basket_context_json`, containing:

```json
{
  "primary": {
    "name": "MEB",
    "description": "...",
    "scope": "national",
    "scope_label": "National",
    "statistics": {}
  },
  "secondary_included": true,
  "secondary": {
    "name": "Pastoral Basket",
    "description": "...",
    "scope": "selected_regions",
    "regions": ["Region A", "Region B"],
    "statistics": {}
  },
  "direct_absolute_comparison_allowed": false
}
```

### 11.1 Highlights prompt

Replace the singular `Food Basket Cost` instruction with:

- primary basket name, cost, MoM, and YoY first;
- secondary basket name, cost, MoM, and YoY second when included;
- no secondary mention when unchecked;
- one fact set per basket;
- no swapping of names or values;
- no sum/average of baskets;
- no national wording for regional scope;
- comparison only when the structured flag allows it.

### 11.2 Main narrative prompt

The current narrative prompt receives trend analysis but not direct basket ground truth. Add `basket_context_json` directly so MARKET_OVERVIEW and REGIONAL_HIGHLIGHTS do not depend on an LLM-generated intermediary for basket facts.

Rules:

- use the full user-facing basket name at first mention;
- primary precedes secondary;
- use the short description to explain what each basket measures;
- keep descriptions concise and do not embellish beyond user input;
- always state regional scope where applicable;
- avoid ambiguous `the food basket` when two are included;
- do not mention the saved secondary when it is unchecked;
- do not imply that the primary was recalculated over secondary regions.

### 11.3 Regional narrative

For a regional secondary basket:

- identify selected regions explicitly or use a generated scope label;
- discuss only region/basket combinations with complete data;
- do not call the aggregate national;
- do not compare its cost level with the national primary;
- percentage movements may be described independently.

### 11.4 Optional module prompts

- Fuel and exchange-rate sections may say that pressures could affect both baskets only when their relevant components and quantitative trends support it.
- Livestock should mention a named basket only when animal products belong to it.
- Labour purchasing-power wording remains tied to the explicitly selected primary staple.
- Pass basket names/descriptions where useful, but do not force every optional module to mention both baskets.

### 11.5 Red-team prompt and correction loop

Add checks for:

- name/value association;
- primary/secondary order;
- missing included basket;
- mention of an unchecked basket;
- national wording applied to a regional basket;
- unsupported direct comparison across different scopes;
- mixed component coverage;
- values or percentages copied from the wrong basket;
- invented purpose beyond the CO description;
- ambiguous singular wording.

Fix the correction loop so red-team flags are supplied to the highlights and narrative correction prompts. Do not merely increment the correction counter and clear flags before the next drafting pass.

### 11.6 Localization

- Update all three language templates with the same placeholders and rules.
- Add localized basket-scope labels, captions, warnings, and validation messages to `i18n.py`.
- Regenerate prompt hashes and `translations_of` entries in the manifest.
- Keep basket names and user-provided descriptions unchanged unless an explicit future translation feature is added.

## 12. Report Assembly, Streamlit Rendering, and DOCX

Primary files:

- `app/shared/report_blocks.py`
- `streamlit_shared.py`
- `app/shared/docx_export.py`
- `app/services/market_monitor/i18n.py`

### 12.1 Recommended report order

1. Report title.
2. Highlights.
3. Basket definitions/scope box.
4. Primary and optional secondary basket trend figure(s).
5. Market overview.
6. Commodity analysis.
7. Regional highlights and applicable basket charts.
8. Optional modules.
9. References.

### 12.2 Basket definition block

Add a deterministic report block containing:

- basket name;
- role;
- CO description;
- national or selected-region scope;
- selected regions where relevant;
- concise composition and quantities;
- version ID/number in metadata rather than prose where appropriate.

A definition box can be used initially. A compact basket summary table is preferable if a generic table renderer is added to Streamlit and DOCX.

### 12.3 Figure assembly

- Insert only figure IDs actually produced.
- Primary-only output should remain equivalent to the current report.
- When a national secondary is comparable, use the combined chart.
- When scope differs, insert separate primary and secondary figures.
- Every secondary caption includes the basket name and scope.
- DOCX must not contain a missing-figure placeholder when the secondary is unchecked.

## 13. Run Metadata and Reproducibility

Every completed run should record:

```text
secondary_basket_included
primary basket full version snapshot
secondary basket full version snapshot when included
names and descriptions
scope types and regions
item IDs, names, units, and quantities
cache version
coverage status
reportable month decision
```

Editing or archiving a basket after report completion must not change the result or exported DOCX for the old run.

## 14. Tests

### 14.1 Migration and repository tests

- Existing single-basket data migrates to primary MEB without ID loss.
- Primary and secondary can be active concurrently.
- Saving a new primary supersedes only primary.
- Saving a new secondary supersedes only secondary.
- Primary removal is rejected.
- Secondary archive removes only its active pointer.
- Archived secondary remains available by version ID/history.
- Secondary name and description validation.
- Custom primary description validation.
- National/region scope validation.
- Region names must exist in active country metadata.

### 14.2 API and dispatcher tests

- Plural basket GET returns both roles.
- Compatibility `/basket` returns primary.
- Secondary save and archive endpoints.
- Generation defaults to including an existing secondary.
- Generation can explicitly exclude it.
- Exclusion works without changing saved configuration.
- Stale primary conflict identifies primary.
- Stale included secondary conflict identifies secondary.
- Stale excluded secondary does not block a primary-only run.
- Router and dispatcher responses remain equivalent.

### 14.3 Calculation tests

- Two baskets share a commodity with different quantities and calculate independently.
- Secondary-only commodities do not alter primary cost.
- Additional commodities alter neither basket.
- National primary and national secondary produce distinct series/statistics.
- National primary and regional secondary remain separate.
- Regional secondary uses only configured/run-overlap regions.
- Empty scope overlap blocks inclusion.
- Missing primary target component blocks every report.
- Missing secondary target component blocks only when secondary is included.
- Unchecking secondary restores primary reportable months.
- MoM and YoY require complete corresponding comparison months.
- Regional incomplete components do not produce misleading complete bars.
- Targeted backfill fetches union once and resolves gaps per basket.

### 14.4 UI behaviour tests

- Primary defaults to MEB.
- Custom primary name requires description.
- Secondary add form requires name and description.
- Region selector appears only for regional scope.
- Saved secondary inclusion checkbox defaults on.
- Unchecking applies to one run only.
- Next new iteration defaults it back on.
- Removing secondary requires confirmation and hides inclusion control.
- Changing country or secondary version resets inclusion to on.

### 14.5 Prompt and report tests

- Primary-only prompt/output remains singular.
- Two-basket output names both and preserves order.
- Unchecked secondary is absent from prompt context and draft.
- Regional secondary is never described as national.
- Different-scope absolute comparison is forbidden.
- CO descriptions appear as context without unsupported expansion.
- Deliberately swapped values are flagged by red-team QA.
- Correction prompts receive and act on QA flags.
- All prompt manifests validate in English, French, and Spanish.
- Report blocks and DOCX contain correct basket definitions and figures.

### 14.6 Regression tests

- Current primary calculation and graph fixtures continue passing.
- Current API clients using `basket_version_id` continue working.
- Labour staple remains selected from the primary basket.
- FX diagnostics remain based on primary data.
- Async run status/result/export remains functional.

## 15. Implementation Sequence

### Phase 1 — Schema and basket domain

1. Add SQLite/PostgreSQL migration.
2. Extend dataclasses and validation models.
3. Add role-aware repository operations.
4. Add secondary archive behaviour.
5. Add migration/repository tests.

Exit condition: both roles can be stored, versioned, retrieved, and archived correctly.

### Phase 2 — API and dispatcher

1. Add plural basket endpoints.
2. Keep primary compatibility endpoint.
3. Extend report input/output schemas.
4. Mirror routes in dispatcher.
5. Add version-conflict and archive tests.

Exit condition: both backend paths expose identical two-role behaviour.

### Phase 3 — Configuration and run UI

1. Refactor shared basket editor logic.
2. Add primary metadata/scope fields.
3. Add secondary create/edit/archive UI.
4. Add default-on per-run inclusion control outside the report form.
5. Implement explicit iteration reset behaviour.
6. Add scope/run-region validation.

Exit condition: a CO can persist both baskets and exclude the secondary for one report without deleting it.

### Phase 4 — Data calculation and reportability

1. Add role-aware basket calculation specs.
2. Retrieve union price requirements.
3. Produce separate national/regional basket series.
4. Enforce complete coverage for target/MoM/YoY.
5. Add joint reportable-month and refresh logic.
6. Preserve primary compatibility aliases.
7. Add calculation and missing-data tests.

Exit condition: basket values cannot be merged, mislabeled, or silently calculated from partial components.

### Phase 5 — Graphs and report assembly

1. Add role-aware chart generation.
2. Add scope-aware chart selection.
3. Add basket definition block/table.
4. Update Streamlit renderer and DOCX export if a new table kind is used.
5. Preserve primary-only layout.

Exit condition: primary-only, comparable two-national-basket, and national-plus-regional-secondary reports render correctly.

### Phase 6 — Prompts, optional modules, and QA

1. Add deterministic basket context.
2. Update trend, highlights, narrative, regional, and red-team prompts.
3. Update optional module context and explicit primary-only labour selection.
4. Fix correction-loop flag delivery.
5. Update translations, i18n, and prompt manifest.
6. Add narrative and deliberate-error QA tests.

Exit condition: every numerical basket statement is unambiguous, scope-correct, and QA-verifiable.

### Phase 7 — Regression, rollout, and documentation

1. Run focused basket, loader, graph, i18n, API, async, and export tests.
2. Run the full test suite.
3. Verify migration against a copy of an existing database.
4. Generate and visually inspect three sample reports:
   - primary only;
   - primary plus national secondary;
   - national primary plus regional secondary.
5. Update Price Bulletin user instructions.
6. Release behind a feature flag if operational rollback is desirable.

Exit condition: the feature is reversible, documented, and does not regress current single-basket reports.

## 16. Acceptance Criteria

The feature is complete when all of the following are true:

1. A country cannot generate a report without a valid primary basket.
2. An unnamed primary defaults to `MEB`.
3. A custom primary requires a description.
4. A secondary requires a name, description, scope, composition, and positive quantities.
5. Both baskets persist independently across sessions and report iterations.
6. The saved secondary defaults to included for every new report iteration.
7. A user can exclude it for one run without editing or deleting it.
8. The following iteration defaults it back to included.
9. A secondary can be archived without affecting previous reports.
10. A primary cannot be archived or removed.
11. National and regional basket values are never mislabeled or directly compared when scopes differ.
12. The primary is not recalculated over the secondary scope.
13. Shared commodities with different quantities are calculated independently.
14. Incomplete basket components never silently produce a complete-looking cost or percentage.
15. Reportable months respond to secondary inclusion state.
16. Basket name, description, scope, cost, MoM, and YoY stay associated through prompts and outputs.
17. Primary-only reports remain backward compatible.
18. English, French, and Spanish prompt manifests and outputs pass validation.
19. Streamlit, JSON, async results, and DOCX output all reflect the same basket selection and facts.
20. Old reports remain reproducible after either basket is edited or the secondary is archived.

## 17. Principal Risks and Mitigations

### Risk: secondary exclusion remains off because of Streamlit state

Mitigation: define a report-iteration reset token/key and explicitly reset inclusion to true after submission/new iteration, country change, or secondary-version change.

### Risk: shared commodities are merged

Mitigation: calculate from independent role specs; de-duplicate only retrieval IDs, never calculation items.

### Risk: regional basket is narrated as national

Mitigation: carry scope in every statistics object and prompt, use scope-specific chart titles, and add a red-team rule.

### Risk: partial baskets generate misleading trends

Mitigation: store component completeness for every month/region and suppress costs or comparisons that do not meet policy.

### Risk: legacy clients break

Mitigation: retain primary-only endpoint, input, output, and `FoodBasket` aliases during a documented compatibility period.

### Risk: old report values change after basket edits

Mitigation: immutable basket versions plus full run snapshots; never resolve an old run through the current pointer.

### Risk: LLM conflates basket purpose or values

Mitigation: deterministic role-keyed context, explicit comparison flag, one fact block per basket, red-team association checks, and corrected feedback loop.

## 18. Phase 7 Release Runbook

### Operational feature gate

- `MARKET_MONITOR_SECOND_BASKET_ENABLED` defaults to `true`. The values `0`, `false`, `no`, and `off` disable new secondary-basket activity.
- Disabled mode keeps primary configuration and primary-only reports available. Existing secondary configuration and history remain readable, and completed two-basket runs remain viewable and exportable.
- Secondary save/archive and explicit secondary reportability or generation return HTTP 503 with code `second_basket_disabled`. A generation request that omits all secondary fields is normalized to primary-only; an explicitly excluded secondary ID remains ignored.
- The FastAPI and dispatcher service-information responses expose `features.second_food_basket`; the plural configuration envelope exposes `second_basket_enabled` for Streamlit and other clients.
- The flag is checked when a request or async run is accepted. An already accepted async run continues with its captured immutable selection.

### Release qualification commands

Use the repository virtual environment and an isolated output directory. Never pass the configured production database to a rehearsal command.

```powershell
# Focused two-basket, loader, graph, API, async, UI, i18n, and export regression suite
& 'venv\Scripts\python.exe' -m pytest -q `
  tests/test_country_food_basket.py `
  tests/test_market_monitor_basket_calculation.py `
  tests/test_market_monitor_basket_ui.py `
  tests/test_market_monitor_databridges_loader.py `
  tests/test_market_monitor_graph_fx.py `
  tests/test_market_monitor_i18n.py `
  tests/test_market_monitor_phase5.py `
  tests/test_market_monitor_phase6.py `
  tests/test_market_monitor_phase7.py `
  tests/test_price_bulletin_basket_ui.py `
  tests/test_price_cache_api.py `
  tests/test_price_cache_repository.py `
  tests/test_price_cache_refresh_worker.py `
  tests/test_live_async_visibility.py `
  tests/test_async_run_artifacts.py

# Full suite. TEST_POSTGRES_DATABASE_URL must point to a disposable PostgreSQL test database.
$env:TEST_POSTGRES_DATABASE_URL='<runtime-only disposable URL>'
& 'venv\Scripts\python.exe' -m pytest -q -rs
```

The PostgreSQL migration/repository test must execute during release qualification. Only the credential-gated live DataBridges test (`tests/test_databridges_adapter.py`) may remain skipped; the second one went with the unused DataBridges client in September 2026.

### Populated migration rehearsals

First create sanitized, populated copies at schema version 004. The SQLite command copies its source before applying migration 005. PostgreSQL must already be restored into a disposable clone or isolated database; the command refuses a URL matching `PRICE_CACHE_DATABASE_URL`.

```powershell
& 'venv\Scripts\python.exe' scripts/phase7_second_basket_qa.py verify-sqlite `
  --source '<populated-v004-copy.sqlite3>' `
  --output-dir '.tmp/phase7-second-basket'

$env:PHASE7_POSTGRES_CLONE_DATABASE_URL='<runtime-only disposable clone URL>'
& 'venv\Scripts\python.exe' scripts/phase7_second_basket_qa.py verify-postgres `
  --confirm-disposable DISPOSABLE `
  --output-dir '.tmp/phase7-second-basket'
```

Both audits must report identifier preservation, primary MEB/national backfills, role-matched pointer constraints, migration idempotency, independent primary/secondary pointers, country-wide sequencing, archive idempotency, and immutable history retrieval. Audit JSON contains counts and identifier hashes, not credentials or source rows.

### Multilingual visual QA

```powershell
& 'venv\Scripts\python.exe' scripts/phase7_second_basket_qa.py generate-samples `
  --output-dir '.tmp/phase7-second-basket'

# Optional deterministic database for refreshing the user-guide screenshot through the real Streamlit page.
& 'venv\Scripts\python.exe' scripts/phase7_second_basket_qa.py prepare-ui-fixture `
  --output-dir '.tmp/phase7-second-basket'
```

The screenshot fixture contains only synthetic South Sudan basket definitions. Run Streamlit with that SQLite file,
capture the rendered primary and secondary summary cards plus the inclusion control, visually inspect the image, and
replace `app/shared/assets/price_bulletin_drafter.jpeg`. Never capture a production database or user-authored report.

Inspect every rendered page at 100 percent zoom for:

- English national primary-only report;
- French national primary plus national secondary report;
- Spanish national primary plus selected-region secondary report;
- readable definition-table rows and unchanged CO-authored descriptions;
- explicit scope in chart titles/captions and correct role ordering;
- incomplete historical points shown as gaps;
- no duplicate compatibility figures, empty placeholders, clipped content, or broken tables;
- narrative names, values, and scope associated with the correct basket.

After inspection, record the outcome:

```powershell
& 'venv\Scripts\python.exe' scripts/phase7_second_basket_qa.py record-review `
  --output-dir '.tmp/phase7-second-basket' `
  --reviewer '<reviewer>' `
  --status passed `
  --notes '<rendering tool and any observations>'
```

All generated databases, JSON payloads, DOCX files, rendered pages, and review manifests remain under ignored `.tmp/phase7-second-basket/` and are not publication artifacts.

### Rollout and rollback

1. Back up the deployment database and pass the SQLite and PostgreSQL rehearsal gates.
2. Deploy the current code with `MARKET_MONITOR_SECOND_BASKET_ENABLED=false`; allow migration 005 to apply and run primary-only Streamlit, FastAPI, dispatcher, async, and DOCX smoke tests.
3. Set the flag to `true`; verify secondary configuration, joint reportability, sync/async immutable selection, role-specific figures/narratives, QA metadata, and export.
4. Monitor async failures, role-specific reportability conflicts, unresolved QA warnings, and DOCX export errors.
5. For operational rollback, set the flag to `false` and repeat the primary-only smoke test. Do not delete secondary pointers or versions and do not run a down migration.

Deploying a pre-Phase-1 binary against a database with secondary current pointers is not an approved rollback path. Singular basket endpoints and primary input/output aliases remain supported without a removal deadline in this release.

### Acceptance evidence matrix

| # | Acceptance criterion | Required evidence |
|---|---|---|
| 1 | Primary required for real generation | FastAPI/dispatcher generation conflict tests and Streamlit missing-primary test |
| 2 | Blank primary defaults to `MEB` | Repository and basket-UI normalization tests |
| 3 | Custom primary requires description | Repository and API validation tests |
| 4 | Secondary requires complete valid configuration | Repository, helper, API, and Streamlit editor tests |
| 5 | Both roles persist independently | SQLite/PostgreSQL rehearsal and concurrent-active repository tests |
| 6 | New iterations default secondary inclusion on | Streamlit iteration-state tests |
| 7 | One run can exclude secondary | Streamlit payload and generation selection tests |
| 8 | Following iteration resets inclusion on | Completed/failed async iteration tests |
| 9 | Archive preserves previous reports | Repository archive/history and stored-result export tests |
| 10 | Primary cannot be archived | Repository validation test |
| 11 | Scope labels and comparisons remain correct | Calculator, graph, prompt, QA, and multilingual sample review |
| 12 | Primary scope remains independent | National/regional calculator tests |
| 13 | Shared commodities retain independent quantities | Calculator contribution tests |
| 14 | Incomplete components cannot look complete | Coverage gate, statistics, graph-gap, and sample-review checks |
| 15 | Reportability follows inclusion | Reportability, refresh, cache-key, and Streamlit tests |
| 16 | Basket facts remain associated | Deterministic context, prompt capture, red-team, and sample-review checks |
| 17 | Primary-only compatibility | Singular API, legacy request/result, flag-off, and numerical regression tests |
| 18 | English/French/Spanish parity | Prompt-manifest/i18n tests and one visual scenario per locale |
| 19 | Streamlit/JSON/async/DOCX selection parity | API/dispatcher/AppTest/export tests and sample manifests |
| 20 | Old reports remain reproducible | Immutable lookup, archive, stored-result normalization, flag-off retrieval/export tests |

Phase 7 is complete only when every matrix row has passing evidence, both populated migration rehearsals pass, the PostgreSQL test is not skipped, the full suite has no unexpected failures/skips, all three visual reviews are recorded as passed, and primary-only smoke testing succeeds with the feature gate disabled.
