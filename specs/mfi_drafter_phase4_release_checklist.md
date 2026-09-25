# MFI Drafter 2.0 Phase 4 Release Checklist

> **Historical — superseded 2026-09.** This document describes the claim-based MFI workflow, which the September 2026 coherence refactor removed (`coherence_refactor_plan.md`, Phase 3). It is kept as a dated record; the current workflow is described in `mfi_light_workflow.md`.

Release ID:

Candidate revision:

Legacy reference revision: `524cc9a`

Validation configuration hash:

Reviewer:

Review date:

## Configured regression cases

Complete one copy of the approval section below for every case marked
`required_for_release` in the local validation configuration. The source CSV
and configuration remain local and untracked.

Case ID:

Case label:

- [ ] Deterministic regression has no blocking failure.
- [ ] All authoritative Level-1 and overall scores match the configured CSV.
- [ ] Configured market counts, exclusions, priority dimensions, warnings, and
      optional metric assertions match.
- [ ] Competition polarity, Quality applicability, category/item separation,
      coverage, ledger links, and claim traceability are correct.
- [ ] All nine dimensions appear exactly once.
- [ ] Food Quality uses question evidence rather than invented subdimensions.
- [ ] Cited claims are immediately followed by readable evidence notes.
- [ ] Priority tables and charts use the correct scale, polarity, scope, and
      coverage.
- [ ] Preview and DOCX have the same hierarchy and no clipping or unreadable
      content.
- [ ] No unsupported national or risk terminology appears in the presentation.
- [ ] Expected limitations are visible and accepted.
- [ ] Every warning not declared in the case configuration has a disposition.

Decision: `approved` / `rejected`

Notes:

## Automated repository gates

- [ ] At least one required regression case is configured.
- [ ] Every configured required case has regression evidence and approval.
- [ ] API and dispatcher compatibility tests pass.
- [ ] Repository-wide test suite passes.

## Live pilot

- [ ] One integrated asynchronous run is approved for every configured case
      marked `requires_live_pilot`.
- [ ] The configured number of distinct real asynchronous assessments is
      approved.
- [ ] Every qualifying run used the configured LLM and no drafting fallback.
- [ ] Preview and DOCX exports succeeded for every run.
- [ ] No score-integrity failure or unresolved high-severity QA flag remains.
- [ ] Every medium-severity flag has a recorded disposition.

## Default rollout and observation

- [ ] `MFI_DRAFTER_ANALYSIS_VERSION=2` enabled in the target deployment.
- [ ] Prior deployment revision remains available for rollback.
- [ ] First ten completed reports monitored.
- [ ] At least seven calendar days observed.
- [ ] Failures, fallback use, warning codes, QA, corrections, exports, and
      version metadata reviewed.

Final decision: `release` / `hold` / `rollback`

Final approver:

Decision date:
