"""Bounded model calls with section-level repair, bookkept in memory for one run."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import logging
import threading
import time

from app.shared.llm import (EMPTY, INVALID_JSON, TRUNCATED_FINISH_REASONS, LLMCallError, LLMRequest,
    ModelProfile, is_transient, log_llm_run_summary)
from .errors import MFIRunError
from .reliable_contracts import fingerprint
from .light_contracts import (MODEL, NODES, WORKFLOW, MAX_CHARACTERS, MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS, ReviewResponse, dumps, instructions, inspect_sections,
    parse_response, response_schema)

logger = logging.getLogger(__name__)

# Each attempt is a single call: ModelRuntime chooses between retry and repair within two attempts.
PROFILE = ModelProfile(service="mfi-drafter", model=MODEL, location="global", temperature=1.0,
                       timeout_seconds=600, max_output_tokens=MAX_OUTPUT_TOKENS, candidate_count=1)
SUMMARY_TIMEOUT_SECONDS = 180
COUNT_TIMEOUT_SECONDS = 30


class Oversized(MFIRunError):
    def __init__(self, characters, tokens=None):
        super().__init__(f"MFI request exceeds its budget ({characters} characters, {tokens} tokens); subdivide the requested sections", 422)


class Stopped(MFIRunError):
    def __init__(self):
        super().__init__("Another step of this report failed; this step starts no further model calls", 409)


class InvalidResponse(ValueError):
    pass


def rejection(error):
    """Why the model's reply was refused, as the repair request tells the model; None when the reply is not at fault."""
    if isinstance(error, InvalidResponse):
        return str(error)
    if isinstance(error, LLMCallError):
        if isinstance(error.__cause__, InvalidResponse):
            return str(error.__cause__)
        if error.failure_code in {EMPTY, INVALID_JSON}:
            return "Model output is not valid JSON"
    return None


def retryable(error):
    """A refused reply is repaired and a transient provider error retried; anything else fails the run."""
    if rejection(error) is not None:
        return True
    if isinstance(error, LLMCallError):
        return error.stage == "transport" and error.transient
    return is_transient(error)


class RunLedger:
    """Phase and work records of one run; kept in memory and discarded with the run."""

    def __init__(self, run_id):
        self._lock = threading.RLock()
        self._value = {"run_id": run_id, "execution_state": "running"}

    def read(self):
        with self._lock:
            return deepcopy(self._value)

    def change(self, mutation):
        with self._lock:
            mutation(self._value)

    def finish(self, state):
        self.change(lambda value: value.update(execution_state=state))


def public_diagnostics(manifest, trace):
    """Phase and work progress of a run and its call trace, without prompt or reply text."""
    work = manifest.get("light_work", {})
    phases = manifest.get("light_phases", {})
    attempts = Counter(call.get("work_item") for call in trace["calls"])
    phase_rows = [{"node": name, "status": phases.get(name, {}).get("status", "pending"),
                   "reused": phases.get(name, {}).get("reused", False)} for name in NODES]
    counts = Counter(row["status"] for row in phase_rows)
    state = manifest["execution_state"]
    return {"narrative_orchestration_version": "mfi-light-v1", "model": MODEL,
        "phases": phase_rows, "work_totals": {"planned": len(NODES), **{k: counts[k] for k in ("pending", "running", "succeeded", "failed")}},
        "progress_pct": int(100 * counts["succeeded"] / len(NODES)),
        "model_attempt_total": trace["total_calls"], "token_count_requests": manifest.get("light_token_count_requests", 0),
        "batches": [{"work_id": key, "status": w["status"], "section_ids": w.get("section_ids", []),
                     "attempt_count": attempts[key], "issues": w.get("issues", [])} for key,w in work.items()],
        "reviews": manifest.get("light_review_outcomes", {}),
        "llm_diagnostics": {**trace, "status": "completed" if state == "completed" else "failed" if state in {"failed", "interrupted"} else "running"}}


class Reporter:
    """Pushes a run's progress and call trace to the live view while it runs, and its final trace once."""

    def __init__(self, ledger, tracer, on_step=None, trace_sink=None):
        self.ledger, self.tracer = ledger, tracer
        self.on_step, self.trace_sink = on_step, trace_sink
        self._lock = threading.RLock()

    def __call__(self, name=None, value=None):
        with self._lock:
            manifest = self.ledger.read()
            if manifest["execution_state"] != "running":
                return  # A sibling that finishes after a failure must not overwrite the failed run.
            diag = public_diagnostics(manifest, self.tracer.snapshot())
            if self.trace_sink:
                self.trace_sink(diag["llm_diagnostics"])
            if self.on_step:
                self.on_step(name or "model_call", {**(value or {}), "workflow_revision": WORKFLOW,
                    "generation_diagnostics": {k:v for k,v in diag.items() if k != "llm_diagnostics"},
                    "llm_diagnostics": diag["llm_diagnostics"]})

    def finish(self, state):
        """End the run: its final call trace goes to the live view and the logs, and later pushes are ignored."""
        with self._lock:
            self.ledger.finish(state)
            diag = public_diagnostics(self.ledger.read(), self.tracer.snapshot())
            if self.trace_sink:
                try:
                    self.trace_sink(diag["llm_diagnostics"])
                except Exception:
                    # The run's outcome stands even when its live view misses the last update.
                    logger.exception("MFI run %s: the final LLM diagnostics were not pushed", self.tracer.run_id)
        log_llm_run_summary(diag["llm_diagnostics"])
        return diag


class ModelRuntime:
    def __init__(self, ledger, llm):
        self.ledger = ledger
        self.llm = llm

    def budget(self, prompt, schema, node):
        """Admit a request while the run can still complete, and only within its character and token limits."""
        manifest = self.ledger.read()
        if any(phase.get("status") == "failed" for phase in manifest.get("light_phases", {}).values()):
            raise Stopped()
        payload = {"model": PROFILE.model, "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generation_config": {"response_schema": schema, "response_mime_type": "application/json",
                                  "temperature": PROFILE.temperature, "max_output_tokens": PROFILE.max_output_tokens}}
        characters, key = len(dumps(payload)), fingerprint(payload)
        if characters > MAX_CHARACTERS:
            raise Oversized(characters)
        tokens = manifest.get("light_token_counts", {}).get(key)
        if tokens is None:
            request = LLMRequest(operation="mfi.light."+node+".v1", node=node, parts=[prompt], response_schema=schema,
                                 json_output=True, timeout_seconds=COUNT_TIMEOUT_SECONDS)
            for attempt in range(2):
                self.ledger.change(lambda v: v.update(light_token_count_requests=v.get("light_token_count_requests", 0)+1))
                try:
                    tokens = self.llm.count_tokens(request)
                    break
                except Exception as exc:
                    if attempt or not retryable(exc):
                        raise
                    time.sleep(1)
            self.ledger.change(lambda v: v.setdefault("light_token_counts", {}).update({key: tokens}))
        if tokens > MAX_INPUT_TOKENS:
            raise Oversized(characters, tokens)
        return characters, tokens, key

    def invoke(self, node, work_id, package, section_ids, *, review=False):
        timeout = SUMMARY_TIMEOUT_SECONDS if node == "executive_summary" else PROFILE.timeout_seconds
        schema = response_schema(review)
        prompt = instructions(node) + "\nREQUEST:\n" + dumps(package)
        _, _, dependency = self.budget(prompt, schema, node)
        key = work_id + ":" + dependency[:20]
        self.ledger.change(lambda v: v.setdefault("light_work", {}).setdefault(key, {
            "status": "pending", "section_ids": section_ids, "issues": [], "node": node}))
        accepted, issues, notes = {}, [], []

        def consume(payload, finish_reason, requested):
            nonlocal issues
            truncated = str(finish_reason).upper() in TRUNCATED_FINISH_REASONS
            if review:
                if truncated:
                    raise InvalidResponse("Review output was truncated")
                try:
                    result = ReviewResponse.model_validate(payload).model_dump()
                    if not result["review_markdown"].strip():
                        raise ValueError("Empty review")
                    _, citation_issues = inspect_sections({"sections": [{"section_id": "review", "text_markdown": result["review_markdown"]}]},
                        ["review"], package.get("EVIDENCE", {}).get("sources", {}))
                    if citation_issues:
                        raise ValueError("Unavailable source citation in review")
                    return result
                except ValueError as exc:
                    raise InvalidResponse("Review requires a boolean needs_revision, nonempty review_markdown and available source citations") from exc
            valid, issues = inspect_sections(payload, requested, package.get("EVIDENCE", {}).get("sources", {}))
            if truncated:
                # The final section may end mid-sentence even if the provider
                # closed its JSON envelope. Earlier complete sections are safe
                # to keep; request the potentially cut section again.
                rows = payload.get("sections", []) if isinstance(payload, dict) else []
                last = rows[-1].get("section_id") if rows and isinstance(rows[-1], dict) else None
                valid.pop(last, None)
                accepted.pop(last, None)
                issues.append("Model output was truncated; complete the remaining sections and envelope")
            accepted.update(valid)
            if isinstance(payload, dict) and isinstance(payload.get("notes"), list):
                notes.extend(n for n in payload["notes"] if isinstance(n, str))
            self.ledger.change(lambda v: v["light_work"][key].update(issues=list(issues)))
            if issues:
                raise InvalidResponse("; ".join(issues))
            return {"sections": [{"section_id": sid, "text_markdown": accepted[sid]} for sid in section_ids],
                    "notes": list(dict.fromkeys(notes))}

        failed, repair = None, False  # the first attempt's failed call, and whether its reply was refused
        for attempt_number in range(2):
            requested = section_ids if review else [sid for sid in section_ids if sid not in accepted]
            outgoing = prompt
            if issues or accepted:
                repair_package = {**package, "requested_sections": requested,
                    "response_errors": issues, "instruction": "Return only the requested sections. Other sections are already saved. If none are requested return sections: [] and notes: [] to repair the envelope only."}
                outgoing = instructions(node) + "\nREQUEST:\n" + dumps(repair_package)
            try:
                self.budget(outgoing, schema, node)
            except Oversized as exc:
                # Do not turn an oversized repair into a new whole-draft call.
                if accepted or attempt_number:
                    raise MFIRunError("The remaining response repair cannot fit its request budget", 422) from exc
                raise
            self.ledger.change(lambda v: v["light_work"][key].update(status="running"))
            request = LLMRequest(operation="mfi.light."+node+".v1", node=node, parts=[outgoing],
                response_schema=schema, json_output=True, timeout_seconds=timeout, work_item=key,
                retry_of=None if repair else failed, repair_of=failed if repair else None)
            try:
                result = self.llm.generate(request, parse=parse_response,
                    validate=lambda payload, response: consume(payload, response.finish_reason, requested))
            except Exception as exc:
                reason = rejection(exc)
                self.ledger.change(lambda v: v["light_work"][key].update(status="failed", issues=[reason] if reason else []))
                if attempt_number or not retryable(exc):
                    raise
                issues, failed, repair = [reason] if reason else [], getattr(exc, "call_id", None), reason is not None
                time.sleep(1)
                continue
            return self.complete(key, result.value)
        raise MFIRunError(f"Attempts exhausted for {node}; generate the report again", 502)

    def complete(self, key, output):
        self.ledger.change(lambda v: v["light_work"][key].update(status="succeeded", issues=[]))
        return output
