# MFI Drafter — R0 regression tooling

> **Historical — superseded 2026-09.** This document describes the claim-based MFI workflow, which the September 2026 coherence refactor removed (`coherence_refactor_plan.md`, Phase 3). It is kept as a dated record; the current workflow is described in `mfi_light_workflow.md`.

Phase R0 of the MFI Drafter 2.0 remediation (`MFI Manuals/MFI_Drafter_2_0_fix.html`,
section 17) builds the measuring instruments every later phase is gated on. It changes no
production behaviour. This document is the contract those phases rely on.

## What R0 provides

| Module | Purpose |
| --- | --- |
| `app/services/mfi_drafter/report_inspector.py` | Pure measurement of report blocks, DOCX exports, and analysis profiles. Importable anywhere, with a CLI. |
| `app/services/mfi_drafter/deterministic_report.py` | Runs the full report path with no LLM, from a CSV, DataFrame, or loaded payload, capturing chart titles. |
| `app/services/mfi_drafter/synthetic_fixtures.py` | Parameterised synthetic assessments with configurable coverage and injectable defects. |
| `app/services/mfi_drafter/r0_diagnostic.py` | Baseline snapshot of the diagnostic assessment's analytical identity. |

None of these are imported by `router.py`, `graph.py`, or any request path. They are local
tooling, in the same position as `release_validation.py`.

## The gate command

```powershell
$env:MPLBACKEND = "Agg"
& 'venv\Scripts\python.exe' -m pytest -q -rs -rx `
    tests/test_mfi_r0_synthetic.py tests/test_mfi_r0_inspector.py `
    tests/test_mfi_r0_artifact.py tests/test_mfi_r0_diagnostic.py
```

Run it **before and after every phase**. `-rs` prints every skip reason, so a run that
skipped everything cannot be mistaken for a real pass. `-rx` prints the outstanding defect
ledger, which is the phase's evidence of what remains.

Drop the last two paths on a machine without local data; that subset needs no assessment
file and finishes in a few seconds.

## Environment contract

| Variable | Default | Effect |
| --- | --- | --- |
| `MFI_R0_DIAGNOSTIC_CSV` | first `MFI Test Databases/*Gaza*.csv` | Live-mode input. |
| `MFI_R0_ARTIFACT_DOCX` | first `.tmp/mfi-review-*/*.docx` | Artifact-mode input. |
| `MFI_R0_BASELINE_DIR` | `.tmp/mfi-r0` | Snapshot root. Must resolve under `.tmp`. |
| `MFI_R0_UPDATE_BASELINE` | unset | Re-record the snapshot deliberately. |
| `MFI_R0_REQUIRE_DIAGNOSTIC` | unset | Turn "input absent" skips into failures. Set this in a phase checklist. |

Assessment data and generated reports are never committed. `MFI Test Databases/` and
`.tmp/` are both gitignored.

## Three inspection modes

Defects are not all visible in the same place, and using the wrong mode silently measures
nothing.

| Mode | Reads | Sees |
| --- | --- | --- |
| Profile | `build_assessment_profile` output | Evidence-availability limitations, decided before any rendering. |
| Live | Blocks, DOCX, and captured chart titles from a no-LLM run | Chart coverage, table geometry, section structure, note repetition. |
| Artifact | A stored DOCX from a real generation run | Markdown leakage and unresolved QA findings, which fallback narratives never produce. |

Three facts drive the split:

- **Chart titles are rasterised into the figure images.** `Coverage: 0/0 markets` appears
  nowhere in the document text; it is observable only by intercepting `ax.get_title()`
  while figures render. `capture_chart_titles()` wraps `graph.save_plot_to_base64` and
  reads titles *before* delegating, because the original closes the figure.
- **Markdown leakage and unverified claims come from model output.** A deterministic run
  produces neither, so those measurements need a stored artifact.
- **QA warnings render as single-cell tables**, not paragraphs. A text extractor that only
  walks `document.paragraphs` reports zero. The inspector keeps `paragraph_char_count` and
  `table_cell_char_count` separate so a zero is never ambiguous.

## The defect ratchet

Every known defect has exactly one test, asserting the **fixed** state, marked
`xfail(strict=True)` with the owning phase in its reason:

```python
@pytest.mark.xfail(strict=True, reason="R0 ledger: … (FIX-04, fixed in R5)")
def test_dimension_charts_report_real_coverage(diagnostic_report):
    assert diagnostic_report.zero_coverage_chart_count == 0
```

It xfails today. When the owning phase lands the fix the test passes, `strict=True` turns
that XPASS into a failure, and the implementer must delete the marker. Nothing needs to be
remembered, and no defect count is hardcoded.

**When you fix a defect, delete the marker — do not delete the test.**

### Closed

**FIX-06 (optional non-representation reported as unavailable evidence)** — closed in R1.
Its two ratchet markers were removed and the tests now assert the corrected behaviour, in
`tests/test_mfi_r0_diagnostic.py`, `tests/test_mfi_r0_inspector.py`, and the dedicated
`tests/test_mfi_r1_evidence_classification.py`.

**FIX-04 (dimension charts rendered false `0/0` coverage)** — closed in R5. The
diagnostic ratchet is now a passing assertion across all nine dimensions, and dedicated
coverage-contract tests reject missing, malformed, or internally inconsistent typed
coverage before a chart can be delivered.

**FIX-05 (canonical rows exported without a reader-facing projection)** is closed in R6.
Six immutable analytical table specs now select and format report rows before they reach
either renderer; every visible analytical cell retains ledger linkage, all tables declare
at most eight columns, and complete canonical rows remain available through the Technical
details JSON/CSV downloads.

**FIX-07 (empty context section) and FIX-09 (repetitive coverage notes)** are closed in
R7. Every report now discloses a stable retrieval/classification outcome, while one shared
evidence-note composer states claim scope once and emits representation only for items,
applicability-dependent evidence, incomplete fixed metrics, and explicit coverage/count
statistics. The inspector also records total evidence-note word volume.

### Current ledger

| Test location | Defect | Owning phase |
| --- | --- | --- |
| artifact | Operational verdicts on transfer modalities drawn from MFI evidence (FIX-02) | R3 |
| artifact | Unweighted means of market rates worded as respondent shares (FIX-03) | R3 |
| artifact | Markdown delimiters survive into the export (FIX-08) | R3 |
| artifact, inspector | No claim-level validation marker; QA findings never tabulated (FIX-01) | R4 |
| artifact | Historical pre-R8 boilerplate remains in the immutable local artifact; regenerate in R9 (FIX-11) | R9 |
| current renderer, inspector | Dimension boilerplate is replaced by bounded, dimension-specific guidance (FIX-11) | Closed in R8 |

### Not covered by automated measurement

**FIX-10 (overlapping map labels)** is closed in R5 without a text-inspector entry. Label
collision is a geometric property of rendered output, so its regression tests operate on
synthetic sparse, near-coincident, and fully coincident coordinates. They assert the
15-label budget, deterministic rank/name ordering, unchanged source points, collision-free
label boxes, and the reserved edge-lane fallback; the graph smoke test validates the
rendered PNG and compact numbered legend.

Two further measurements are recorded for FIX-02 and FIX-03 in artifact mode only. The
deterministic pipeline writes fallback narratives that contain neither defect, so a live
run cannot demonstrate them; after R3 lands, the same measurements should also be asserted
against a freshly generated report before release.

## Writing assertions

Measurements fall into three classes, and mixing them up produces brittle tests.

**Structural** — safe to pin exactly. `coverage_titled_chart_count == 9` holds on every
dataset because it equals the dimension count.

**Semantic signatures** — the right way to assert a data-dependent defect. The number of
spurious limitations varies (2 on the diagnostic sample, 4 on Benin, 5 on the default
synthetic spec), but "a limitation whose flagged metrics contain no `required` metric" is
invariant. Assert `optional_only_limitation_count` and the dimension set.

**Dataset- or mode-dependent counts** — never assert equality. Boilerplate repeats 4 times
in the artifact but 9 times in a deterministic run, because only some dimensions fell back
to generated prose. Coverage notes repeat 660 times on a 27-market assessment and 366 on a
53-market one. Use bounds, or let the untracked baseline carry the exact figure.

Never match a literal coverage string such as `coverage: 27/27 assessed markets`. Use the
configured named-group pattern and compare `available` against `total`.

## The baseline snapshot

`.tmp/mfi-r0/diagnostic/baseline.json` records the analytical identity of the diagnostic
assessment: market counts, warning codes, the priority set, the mean MFI, and every stored
Level-1 and overall score.

Identity floats are stored as both `repr()` and `float.hex()` and compared bit-exactly via
`float.fromhex`. **No tolerance is applied** — an authoritative score differing in the last
bit is a regression. The `repr` form exists only so a human can read the diff.

Structural measurements are recorded in the same file but are advisory: later phases are
expected to change them, and that difference is the phase's evidence.

Behaviour: absent → record and pass; present with a matching source digest → compare;
present with a different digest → fail and demand `MFI_R0_UPDATE_BASELINE=1`. A snapshot is
never silently re-recorded.

## Known constraints

- **Do not run this suite under `pytest-xdist`.** Chart capture mutates a module attribute
  and matplotlib state is process-global.
- **Loader warnings must be read from `run.loaded`**, not `run.result["warnings"]` — the
  latter is overwritten downstream with a deterministic-rendering note.
- **`report_inspector` must not import the graph stack.** A contract test asserts that
  importing it pulls in no `langgraph`, `langchain_core`, `streamlit`, `matplotlib.pyplot`,
  or `pandas`; it imports in about 0.15 s, versus roughly 5 s for `graph`.
- **`deterministic_report` duplicates `release_validation._deterministic_result`.** The
  duplication is pinned by a parity test until a later phase reduces the older function to
  a delegating wrapper.
