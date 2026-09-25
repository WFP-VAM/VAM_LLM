# MFI Drafter — R3 narrative safety contracts

> **Historical — superseded 2026-09.** This document describes the claim-based MFI workflow, which the September 2026 coherence refactor removed (`coherence_refactor_plan.md`, Phase 3). It is kept as a dated record; the current workflow is described in `mfi_light_workflow.md`.

Phase R3 of the MFI Drafter 2.0 remediation (`MFI Manuals/MFI_Drafter_2_0_fix.html`,
section 17) stops the report stating conclusions the assessment cannot support. It closes
**FIX-02** (modality, affordability and causal overreach), **FIX-08** (Markdown leakage),
and the enforcement half of **FIX-03** (pooled-population wording), building on the
metadata R2 recorded.

## What was wrong

The delivered report contained 20 sentences reaching operational verdicts about transfer
modalities, 55 percentages attributed to "traders" or "responses" that are actually
unweighted means of market rates, and 102 literal Markdown backticks.

None of it was caught, for five separate reasons:

- The market prompt **asked** for a `modality_consideration` field, and three prompt lines
  told the model that hedged modality language was acceptable ("must remain conditional",
  "unilateral").
- The modality validator matched five exact phrases. Every observed violation was a
  conditional variant that passed straight through.
- The affordability check ran only when `artifact_id == "Price"`; the causal check ran only
  on context and executive claims. Dimension and market prose — where the violations
  actually were — was unchecked.
- No text sanitization existed anywhere in the pipeline.
- Nothing enforced the wording R2 recorded.

## One definition, shared

`app/services/mfi_drafter/wording.py` holds the patterns, the sanitizer, the tokenizer and
the deterministic replacement text. It is stdlib-only, so `report_inspector` can import it
without breaking its import-purity contract.

This matters more than it looks. The validator rejects a conclusion; the inspector counts
how many reached the page; the exit gate compares the two. If their definitions diverged,
the gate would be **unsatisfiable** (inspector broader) or **vacuous** (inspector
narrower). A test asserts `InspectorConfig` resolves to the same pattern strings, which is
what keeps the gate falsifiable.

## The rules

**Modality** — a sentence must contain both a modality subject and an operational verdict,
with no methodological negation. Conjunction is evaluated *per sentence*: naming a modality
is not a conclusion, and saying something "warrants review" is not a conclusion. Conditional
modals do not exempt — "could be viable" matches `viable`.

`feasibility` is deliberately **not** a verdict (only `feasible` is): the approved
recommendation vocabulary includes "feasibility assessment", and the deterministic market
recommendation says "triangulate it with operational and feasibility evidence". Including it
would put the safe fallback one noun from flagging itself.

The fix document's contextual-document carve-out is **not** implemented. Its second
condition — that the claim state MFI alone does not determine modality — already earns the
exemption through negation; adding the first would only re-create the permissive path that
made the original five-phrase list fail.

**Affordability and causality** are now ungated. Affordability applies to every artifact,
not just Price, because the observed violations were in market narratives the dimension gate
could never see. Causality applies to every artifact, not just context and executive.

**Negation** is what makes broad rules safe. The pattern recognises a statement of what the
evidence does *not* establish. It has to, because `DIMENSION_DESCRIPTIONS["Price"]` — *"It
does not by itself measure affordability, inflation, or household purchasing power"* — is
injected verbatim into the Price prompt, so the model is actively taught to echo it.
**Before R3 the Price-gated affordability check flagged that echo; R3 fixed a live false
positive while broadening the rule.**

**Population wording** uses R2's metadata. Rule A (high) fires when the text attributes a
percentage to a respondent population *and* a cited value is an unweighted market mean with
no pooled denominator. Rule B (medium) fires when such a value appears as a percentage
without being qualified as unweighted or market-level.

Rule A anchors on the *percentage-of-respondent-noun* construct rather than a bare
respondent noun — R2's own approved phrase for that population is "the unweighted mean of
market-level trader proportions", which contains "trader", so a bare-noun rule would flag the
very wording introduced to fix the defect. The legitimate single-market case passes with no
special-casing: it cites a `market_value` entry, so the predicate is simply false. Limitation
claims are exempt, because the limitation explaining that trader denominators are unavailable
is this rule's own justification.

## The sanitizer

Hooks at `_claim_from_payload`, the single funnel for every drafted claim, so the stored text
is canonical for the API, the preview and the export alike rather than each renderer cleaning
up separately.

Numeric invariance is **enforced, not asserted**: sanitize, re-tokenize, and if the numeric
tokens changed in any way return the original text unchanged for the validator to flag.
Delivering raw text that gets flagged beats silently deleting a digit. Backticks in the
generated report wrap values (`` `35.1%` ``), so this guard is load-bearing.

Never touched: ordered-list markers (`"1. "` is a number), underscores (`cereal_food` appears
in real metric identifiers), `&`, `/`, `%`, parentheses, dashes.

`unsupported_markup` is **medium**, not low: `low` is never emitted deterministically and
nothing consumes it, so a low flag would carry no enforcement weight against an exit gate
that requires zero delimiters.

## Delivery policy

Repair is attempted first, bounded at three passes. `apply_unresolved_claim_policy` runs only
once those are spent, and only on **high** severity — a finding nothing could repair is
precisely the one that most needs replacing, so severity decides rather than repairability.

The claim's text is replaced by deterministic wording keyed on claim kind. Every variant is
digit-free, modality-free and respondent-noun-free, so a replacement can never trip the rules
that caused it — asserted by a test.

`claim_id` is preserved as the join key. Citations are cleared **on the claim** and kept **in
the record**, because an evidence note would otherwise print values beneath text that no
longer states anything about them, and an unresolvable citation is a common cause of
withdrawal in the first place.

Records land in `generation_diagnostics["claim_substitutions"]`, which the Streamlit page
already renders inside technical details — so the rejected draft is retained exactly where the
remediation says and nowhere else. `unmatched_high_claim_ids` records findings that point at
no claim: those are how a report ends up promising traceability it does not deliver.

Medium findings keep their unverified marker and stay on the page, per the delivery policy.

## Other changes worth knowing

- **The per-market modality sentence is gone.** It previously appeared only when drafting
  failed, so whether a market carried the caveat was accidental; and fifteen identical
  sentences are the boilerplate FIX-11 already complains about. The caveat is now stated once
  in the executive summary and once in the methodology note, deterministically, on every run.
- **`validation_flags` and `validation_flag_ids` are now separate.** The validator wrote
  codes, `_mark_unresolved_claims` unioned identifiers into the same list. Identifiers hash the
  flag message and therefore churn whenever wording changes; codes are stable and displayable.
  Downstream consumers should key on codes.
- **Flags now carry the artifact identity of the claim they came from.** The affordability and
  concept checks previously hardcoded `dimension`/`Price`, which was harmless only while they
  were gated to that dimension — correction targets are grouped by artifact identity, so a
  hardcoded one re-drafts the wrong artifact.
- **Terminology matching is word-boundaried.** `"very high risk"` previously produced two
  flags by also matching `"high risk"` inside itself. `"unaffordable"` is still matched, now
  deliberately rather than as a substring accident.

## Verified

On the diagnostic dataset, the deterministic path now yields zero modality conclusions, zero
pooled-population phrases, zero backticks, zero code fences and zero Markdown links, with
`claim_validation.status == "passed"`, mean MFI `5.981638237282082` and priorities
Service/Price/Infrastructure unchanged.

The invariant test is the one to keep working: it runs every string the system produces
without a model — nine dimension fallbacks, market and executive fallbacks, the neutral
statement, every withdrawal variant, every limitation message, every R2 permitted phrase and
every dimension description — through every new rule and asserts none fires. Broad rules are
only safe while that holds, and three separate test files depend on the deterministic path
validating cleanly.

## Not closed by R3

The FIX-02, FIX-03 and FIX-08 ratchet entries remain `xfail`. They measure the **stored**
report, which still contains the old text; they cannot flip without regenerating it, and
regeneration belongs to R9. The ratchet is self-enforcing here: the moment anyone regenerates
with fixed code, `strict=True` turns the XPASS into a failure and forces the markers off.

Rule C of the population policy — requiring a represented-market denominator in the claim or
its adjacent note — is deferred to R7. Evidence notes currently emit coverage unconditionally,
so the rule would be a no-op until R7 rewrites them.
