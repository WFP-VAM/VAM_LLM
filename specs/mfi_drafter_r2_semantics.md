# MFI Drafter — R2 aggregation and representation semantics

> **Partly historical — superseded 2026-09.** The aggregation and representation fields on `MFIMetricLedgerEntry` ("What each entry now carries", "How the values are derived", "Representation basis") still describe the analysis layer. The claim catalogue, phrase rules, prompt exposure and evidence-note renderers belonged to the claim-based workflow, which the September 2026 coherence refactor removed; see `mfi_light_workflow.md`.

Phase R2 of the MFI Drafter 2.0 remediation (`MFI Manuals/MFI_Drafter_2_0_fix.html`,
section 17) records, for every number the system can cite, what population that number
describes and how it should be worded. R2 enforces nothing — R3 does that. This document
is the contract R3 and R7 build on.

## Why

The delivered report says *"35.1% of traders reported scarcity"*. That figure is actually
`1 − (unweighted mean of 27 market-level rates)`. The processed assessment carries only a
per-market `TradersSampleSize` and no applicability-specific respondent denominators, so a
pooled respondent statement cannot be supported by the data at all.

Before R2 the ledger could not express the difference. An assessment-wide mean of per-market
trader rates and a single market's trader proportion were both `statistic="mean_raw_value"`
carrying the same inherited `evidence_scope`. Now they differ in `population_basis`,
`pooled_denominator_available`, and the phrase that describes them.

## What each entry now carries

Five fields on `MFIMetricLedgerEntry`, copied verbatim onto `MFIClaimCatalogEntry`:

| Field | Meaning |
| --- | --- |
| `aggregation_method` | How per-unit values were combined: `unweighted_market_mean`, `market_value`, `rank`, `count`, `coverage`. |
| `population_basis` | The elementary unit behind the denominator: `market_level`, `trader_level_within_market`, `descriptive`. |
| `pooled_denominator_available` | Whether a denominator exists in the data and may be stated numerically. **False for every trader-level value.** |
| `representation_basis` | Which assessed markets stand behind the value (see below). |
| `permitted_subject_phrase` | Deterministic noun phrase describing the value correctly. Never contains a digit. |

The catalog additionally carries `permitted_claim_scopes`, plus `represented_market_count`
and `assessed_market_count` as integers so a claim can cite a denominator without parsing
it back out of the formatted coverage label.

## How the values are derived

`analysis._ledger_semantics()` is a pure function called inside the `add_ledger` closure,
so **every** ledger entry passes through it and complete coverage is a property of the code
path rather than of review diligence. It **raises `ValueError` on an unmapped statistic** —
a new statistic fails loudly at one place instead of silently defaulting.

Resolution is tiered, first match wins:

1. **Statistic only** — `coverage`, `coverage_ratio`, `rank`, `rank_lowest_first`, `count`,
   `denominator`.
2. **Cross-market statistics over official scores** — `mean`, `median`, `minimum`,
   `maximum`, `q1`, `q3`, `iqr`, `range`, `numerator` → `unweighted_market_mean` /
   `market_level`.
3. **Assessment metric aggregates** — `mean_raw_value`, `mean_normalized_value`,
   `derived_unfavorable_rate`. Trader scope → `trader_level_within_market`, pooled `False`.
   This tier is FIX-03.
4. **Single-market values** — `stored_level_1_score`, `market_explanatory_*`,
   `derived_market_unfavorable_rate` → `market_value`.

### Two rules that are easy to get wrong

**Statistic is resolved before evidence scope.** `_materialize_analyzed_metric` stamps one
inherited `evidence_scope` onto all of a metric's ledger entries, so a coverage ratio or a
rank can arrive carrying `surveyed_traders_in_market` even though its denominator is
markets. Tier 1 exists solely to correct that, and it does so without mutating
`evidence_scope`, which `_canonical_scope` and the scope validator still read.

**Aggregation is never derived from `market_name`.** `market.<token>.mfi.rank` carries a
market name but ranks over markets. Deriving `market_value` from the presence of a name
would mislabel every market rank and every market-scoped coverage entry.

### Explicit overrides

Six sites pass `semantics=` or extra derivation inputs, because derivation cannot know what
they mean:

- `assessment.dimension_profile.mean` averages the nine dimension means, not markets.
- The four `count` entries must each name what was counted.
- `_materialize_analyzed_metric` passes R1's `availability` and a subject label, because
  coverage counts alone cannot separate an optional item that was not traded everywhere
  from required evidence that failed to load from a Food Quality condition that did not
  apply. Those three need different wording.

## Representation basis

| Value | Meaning | Phrase suffix |
| --- | --- | --- |
| `all_assessed_markets` | Complete coverage | none — the base phrase already says it |
| `represented_assessed_markets` | Optional item partially traded | "among the assessed markets where {subject} was represented" |
| `applicable_assessed_markets` | Food Quality applicability | "among the assessed markets where {subject} was applicable" |
| `incomplete_assessed_markets` | Required evidence missing or invalid | "among the assessed markets with usable evidence for {subject}" |
| `single_assessed_market` | One market | none |
| `assessment_dimension_profile` | The nine dimension means | none |
| `assessment_input_records` | All input records including excluded | none |

## The phrase rules

**No phrase may contain a digit.** Phrases are sent to the drafting prompts, and numeric
authorization (`narrative._numeric_token_is_authorized`) accepts a number only if it matches
a cited value's own rendering. A digit quoted from a phrase would be rejected as
unauthorized, flipping claim validation to failed and triggering the repair loop.

Consequently, **market and region names are never interpolated** — a market called "Camp 4"
would inject a digit — and subject labels are rejected outright by `_safe_subject` if they
contain a digit *or* a respondent noun. A label such as "At least five traders - Cereal
food" would otherwise make a market-level phrase read as though it counted traders, which is
the exact defect this metadata exists to prevent. Rejected labels fall back to "this item";
consumers still receive `market_name`, `region`, and `label` as separate fields.

Phrases are lowercase noun phrases with no trailing punctuation, so they drop into a sentence
either after "was" or at its start.

The four rows of the wording policy in section 8 of the remediation document map onto:

| Evidence | Phrase |
| --- | --- |
| Assessment mean of market rates | "the unweighted mean market-level unfavorable rate" |
| Single-market trader proportion | "the proportion of surveyed traders in this market" |
| Binary market condition | "the share of assessed markets where this condition was unfavorable" |
| Partial optional item | base phrase + "among the assessed markets where {subject} was represented" |

## Prompt exposure

`compact_catalog` — the single projection feeding all four drafting prompts — carries
`permitted_subject_phrase` and **none of the four classification enums**. The model needs the
wording, not the taxonomy; `validate_structured_narratives` receives the full catalog and
reads the enums directly, so R3's validator needs no extra plumbing.

Measured on the diagnostic sample: prompt payload rose from about 41 KB to about 51 KB per
dimension. A test bounds it at 120 KB, because an oversized prompt makes dimension drafting
fall back to deterministic text, which *is* a narrative change.

## Known costs and deferred work

- **Catalog size.** 5,520 entries, and the JSON grew from about 3.5 MB to about 5.6 MB. It
  already exceeded `_INLINE_RESULT_MAX_BYTES = 900_000` (`app/shared/async_runs.py`) before
  R2 and spills to GCS, which works. Trimming the persisted catalog is a separate change.
- **Enum vocabulary.** `numerator` and the order statistics (`median`, `q1`, `iqr`, …) are
  recorded as `unweighted_market_mean` because the enum has no `sum` or `order_statistic`
  member. `statistic` already distinguishes them and the phrases are accurate, so this is a
  vocabulary gap rather than a defect.
- **Deliberately untouched.** `_format_catalog_value`, `_allowed_renderings` (the numeric
  authorization surface), `_canonical_scope` and `scope` (the scope validator), and both
  evidence-note renderers — `narrative.evidence_note` and
  `app/shared/report_blocks._mfi_evidence_note`, which are near-duplicates and whose output
  the R0 structural block measures. FIX-09's rendering belongs to R7.
- **`permitted_claim_scopes` is pinned to `[scope]`** by a test. R3/R7 widen it so one value
  can back more than one kind of claim, and move the scope validator onto it.
