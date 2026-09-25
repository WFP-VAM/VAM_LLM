# MFI Drafter live narrative QA contract

> **Historical — superseded 2026-09.** This document describes the claim-based MFI workflow, which the September 2026 coherence refactor removed (`coherence_refactor_plan.md`, Phase 3). It is kept as a dated record; the current workflow is described in `mfi_light_workflow.md`.

This contract applies to new MFI Drafter 2.0 live runs. It does not change
authoritative DataBridge values, Phase 2 analysis, release gating, or the ability to
render historical completed results.

## Delivery rule

A live run is publishable only after all of the following are true:

1. every enabled LLM response passed extraction, JSON, and field-specific schema
   validation;
2. deterministic claim validation has no high- or medium-severity findings;
3. every required local and coherence Red-Team batch completed;
4. Red-Team has no unresolved high- or medium-severity findings;
5. canonical claim identities are globally unique;
6. canonical report blocks were built and validated before completion.

Low-severity findings remain advisory. Optional retriever failures remain declared
context limitations. Neither condition authorizes narrative fallback.

## Field correction queue

Material repairable findings are grouped by the exact tuple
`(artifact_type, artifact_id, field_name)`. Each group becomes one sequential task.
The model receives the affected field, read-only artifact context, the applicable
findings, and only authorized evidence. Its response contains one `replacement`
property. The application then applies R8 density limits, reassigns canonical IDs,
validates the complete artifact, and persists the result before advancing.

There are at most three global correction cycles. A field whose finding disappeared is
not called again. A global, unsafe, or non-repairable material finding blocks the run.

## Red-Team distribution

Local review units are canonical `(artifact, field)` pairs. Units are packed in stable
order into packages no larger than 45,000 serialized characters and are never split.
Every claim appears once across local packages. Contract
`mfi-red-team-batches-v2` adds one coherence shard per priority dimension and one per
selected market. Each shard compares that artifact with the complete bounded
executive summary, contains only its cited metric and document evidence, and remains
within the same 45,000-character limit without truncation.

Each package is a persisted graph step. Unchanged signatures retain their prior
finding set after a correction. Stable membership-derived IDs ensure that a market or
dimension correction reruns only its affected local and coherence packages; an
executive-summary correction invalidates every coherence shard. Semantically duplicate
findings about repeated executive claims are consolidated application-side. A failed
or incomplete package blocks the entire run.

## Malformed responses

Only syntactically invalid, non-empty JSON receives one additional LLM syntax
normalization call. The normalizer must preserve all supplied content and cannot add
fields or claims. Empty, unreadable, schema-incomplete, or still-invalid responses
block the run.

## Stable failures

| Code | Meaning | HTTP |
|---|---|---:|
| `llm_call_failed` | Provider, extraction, JSON, or response-contract failure | 502 |
| `mfi_narrative_qa_unresolved` | High/medium QA remains or cannot be targeted safely | 502 |
| `mfi_claim_identity_contract_failed` | Canonical identity invariant failed | 500 |
| `mfi_red_team_batch_contract_failed` | Internal batch package or completion invariant failed | 500 |
| `mfi_report_delivery_contract_failed` | Final canonical blocks could not be validated | 500 |

Failed asynchronous runs remain below 100 percent and expose sanitized task, batch,
call, and operation identifiers where applicable. They have no result, preview, or
DOCX payload.

## Offline fixtures and compatibility

Deterministic narrative generators are reserved for offline regression fixtures. Live
graph, API, dispatcher, and report-construction paths do not import them. Existing
fallback/substitution response fields remain present for compatibility but are empty in
new live results. Historical completed reports containing old withdrawal blocks remain
renderable.
