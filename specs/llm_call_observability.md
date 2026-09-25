# LLM call observability operations

Market Monitor emits metadata-only LLM diagnostics by default. Full prompt and
response capture is opt-in and must use a private Google Cloud Storage prefix
that is not served by any report or artifact endpoint.

The MFI Drafter's light workflow reports its own metadata-only call diagnostics
(counts, sizes, attempts and outcomes) and never stores prompts or responses;
its info and health endpoints still show the tracing configuration. The
Seasonal Outlook keeps each request and response in its private bucket as part
of the analysis audit trail.

## Runtime configuration

- `LLM_TIMEOUT_SECONDS=90`: per-attempt deadline for ordinary LLM calls.
- `LLM_MAX_RETRIES=2`: provider retry limit shared by traced calls.
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

Invalid timeout or retry settings fail report generation before an asynchronous
run is created with stable code `llm_runtime_configuration_invalid`. The MFI
Red-Team operation is `mfi.red_team_review.v4`; it receives a compact,
canonical claim package and uses Vertex controlled JSON generation with a
single response root. Red-Team flag IDs are assigned by the application after
validation and are never accepted from the model.

If the provider returns syntactically malformed JSON, the original response is
held in process memory only and passed once to
`mfi.red_team_response_repair.v1`. That call may normalize formatting but may
not add, remove, or reinterpret findings. Contract-valid repair marks the
initial call as recovered and preserves content-free JSON-shape diagnostics.
An unsuccessful repair, or a semantically incomplete response, fails the run.
No prompt or response body is written to public metadata or logs, and this
recovery path does not require GCS payload capture.
