# LLM call observability operations

Every model call of the app goes through the shared LLM client
(`app/shared/llm/`) and emits metadata-only diagnostics by default: one record
per attempt, JSON log lines on the `app.llm_trace` logger, and a run snapshot
for APIs and the live view. Full prompt and response capture is opt-in and must
use a private Google Cloud Storage prefix that is not served by any report or
artifact endpoint.

The Seasonal Outlook's analysis record is the mandatory audit of its calls: it
stores each attempt's request before the call and its response before
validation, in the Seasonal bucket, and a call fails if either cannot be
stored. Payload capture is therefore off for Seasonal runs.

All three pages show the calls in the same diagnostics panel
(`render_llm_diagnostics`), live while a run or phase is working: the Market
Monitor and MFI pages from the run snapshot, the Seasonal Outlook page from the
call entries of its analysis record.

## Runtime configuration

The two call settings below apply to the Market Monitor. The other drafters'
deadlines and attempts are fixed:
- MFI Drafter: 600 s per call (180 s for the executive summary) and two
  attempts per work item, shared between retries and repairs.
- Seasonal Outlook: the phase timeout the analyst chose (600, 1,200 or
  1,800 s) and two attempts per call, retried only on transient errors.

- `LLM_TIMEOUT_SECONDS=90`: per-attempt deadline for ordinary LLM calls.
- `LLM_MAX_RETRIES=2`: attempts per call, retried only on transient errors
  (timeouts, rate limits, server-side and network failures). As before the
  shared client, 2 means one retry, and 0 or 1 mean a single attempt.
- `LLM_TRACE_PAYLOADS=false` (default): structured call metadata only.
- `LLM_TRACE_PAYLOADS=true`: persist gzip-compressed private payloads.
- `LLM_TRACE_GCS_URI=gs://<private-bucket>/<optional-prefix>`: private storage
  prefix used only by the backend service account.

The Cloud Run service account needs object-create permission on the selected
prefix. Reader access should be limited to backend operators. Do not grant the
application's report users bucket access.

## Required 30-day lifecycle

Apply this lifecycle rule to the trace bucket before enabling payload capture:

```json
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30}
    }
  ]
}
```

Apply the committed `specs/llm_trace_gcs_lifecycle.json` policy with:

```text
gcloud storage buckets update gs://TRACE_BUCKET --lifecycle-file=specs/llm_trace_gcs_lifecycle.json
```

Verify the bucket reports the deletion rule before setting
`LLM_TRACE_PAYLOADS=true`.

Payloads are stored under:

`<configured-prefix>/llm-traces/v1/<service>/<run-id>/<sequence>-<call-id>.json.gz`

The `/info` and `/health` responses expose only whether capture and storage are
configured, their configuration status, and the retention expectation. They do
not expose the bucket name. Per-run diagnostics report persistence failures,
which do not alter a successfully validated model response.

Invalid timeout or retry settings show as invalid in the Market Monitor's
`/info` (`llm_runtime`, stable code `llm_runtime_configuration_invalid`), and a
report run then fails at its first model call.

## MFI retries and repairs

Each MFI work item (a family of sections, a review, or the executive summary)
has two attempts. A transient provider error is retried with the same request;
a reply that is empty, not JSON, truncated, or fails the section contract is
repaired by asking again for the missing or invalid sections only, with the
issues found (never the reply text) in the request. The second call records
`retry_of` or `repair_of`, and when it succeeds the first one shows as
recovered, with disposition `recovered_by_retry` or `recovered_by_repair` and
its content-free JSON-shape diagnostics kept. Any other error, or a second
failure, fails the run. Once any step of a run has failed, the other steps start
no new model call.
