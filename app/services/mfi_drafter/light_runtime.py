"""Bounded model calls with section-level repair, bookkept in memory for one run."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import threading
import time
import uuid

from langchain_core.messages import HumanMessage
from .errors import MFIRunError
from .reliable_contracts import fingerprint
from .light_contracts import (MODEL, NODES, MAX_CHARACTERS, MAX_INPUT_TOKENS,
    MAX_OUTPUT_TOKENS, ReviewResponse, dumps, instructions, inspect_sections,
    parse_response, response_schema)


class Oversized(MFIRunError):
    def __init__(self, characters, tokens=None):
        super().__init__(f"MFI request exceeds its budget ({characters} characters, {tokens} tokens); subdivide the requested sections", 422)


class InvalidResponse(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


_client_lock = threading.Lock()


@lru_cache(maxsize=8)
def _cached_model(project, timeout):
    from langchain_google_vertexai import ChatVertexAI
    return ChatVertexAI(model_name=MODEL, project=project, location="global", temperature=1.0,
                       timeout=timeout, max_retries=0, max_output_tokens=MAX_OUTPUT_TOKENS)


class VertexClient:
    def model(self, timeout):
        from app.shared.llm import _get_vertex_project_id
        with _client_lock:
            return _cached_model(_get_vertex_project_id(), timeout)

    def count(self, messages, schema, timeout):
        model = self.model(timeout)
        prepared = model._prepare_request_gemini(messages, response_mime_type="application/json", response_schema=schema)
        response = model.prediction_client.count_tokens(request={
            "endpoint": prepared.model, "model": prepared.model, "contents": prepared.contents,
            "system_instruction": prepared.system_instruction, "generation_config": prepared.generation_config},
            timeout=30, retry=None)
        return int(response.total_tokens)

    def generate(self, messages, schema, timeout):
        return self.model(timeout).bind(response_mime_type="application/json", response_schema=schema).invoke(messages)


def retryable(error):
    from google.api_core import exceptions as api
    return isinstance(error, (InvalidResponse, TimeoutError, ConnectionError, api.DeadlineExceeded,
        api.ServiceUnavailable, api.InternalServerError, api.TooManyRequests, api.Aborted))


def extracted(response):
    content = response.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    raise InvalidResponse("Response contains no text")


class RunLedger:
    """Phase and model-call records of one run; kept in memory and discarded with the run."""

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


def public_diagnostics(manifest):
    work = manifest.get("light_work", {})
    phases = manifest.get("light_phases", {})
    attempts = sorted([a for w in work.values() for a in w.get("attempts", [])], key=lambda a: a["sequence"])
    calls = [{k:v for k,v in a.items() if k not in {"response_ref", "request_ref", "issues", "requested_ids", "epoch"}} for a in attempts]
    phase_rows = [{"node": name, "status": phases.get(name, {}).get("status", "pending"),
                   "reused": phases.get(name, {}).get("reused", False)} for name in NODES]
    counts = Counter(row["status"] for row in phase_rows)
    return {"narrative_orchestration_version": "mfi-light-v1", "model": MODEL,
        "phases": phase_rows, "work_totals": {"planned": len(NODES), **{k: counts[k] for k in ("pending", "running", "succeeded", "failed")}},
        "progress_pct": int(100 * counts["succeeded"] / len(NODES)),
        "model_attempt_total": len(attempts), "token_count_requests": manifest.get("light_token_count_requests", 0),
        "batches": [{"work_id": key, "status": w["status"], "section_ids": w.get("section_ids", []),
                     "attempt_count": len(w.get("attempts", [])), "issues": w.get("issues", [])} for key,w in work.items()],
        "reviews": manifest.get("light_review_outcomes", {}),
        "llm_diagnostics": {"service": "mfi-drafter", "run_id": manifest["run_id"],
            "status": "completed" if manifest["execution_state"] == "completed" else "failed" if manifest["execution_state"] in {"failed", "interrupted"} else "running",
            "total_calls": len(calls), "succeeded_calls": sum(c["status"] == "succeeded" for c in calls),
            "failed_calls": sum(c["status"] == "failed" for c in calls), "calls": calls}}


class ModelRuntime:
    def __init__(self, ledger, client=None, notify=None):
        self.ledger = ledger
        self.client = client or VertexClient()
        self.notify = notify or (lambda: None)

    def budget(self, messages, schema, timeout):
        payload = {"model": MODEL, "contents": [{"role": "user", "parts": [{"text": m.content}]} for m in messages],
            "generation_config": {"response_schema": schema, "response_mime_type": "application/json",
                                  "temperature": 1.0, "max_output_tokens": MAX_OUTPUT_TOKENS}}
        characters, key = len(dumps(payload)), fingerprint(payload)
        if characters > MAX_CHARACTERS:
            raise Oversized(characters)
        tokens = self.ledger.read().get("light_token_counts", {}).get(key)
        if tokens is None:
            for attempt in range(2):
                self.ledger.change(lambda v: v.update(light_token_count_requests=v.get("light_token_count_requests", 0)+1))
                try:
                    tokens = self.client.count(messages, schema, timeout)
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
        timeout = 180 if node == "executive_summary" else 600
        schema = response_schema(review)
        prompt = instructions(node) + "\nREQUEST:\n" + dumps(package)
        messages = [HumanMessage(content=prompt)]
        characters, tokens, dependency = self.budget(messages, schema, timeout)
        key = work_id + ":" + dependency[:20]
        self.ledger.change(lambda v: v.setdefault("light_work", {}).setdefault(key, {
            "status": "pending", "section_ids": section_ids, "attempts": [], "issues": [], "node": node}))
        accepted, issues, notes = {}, [], []

        def consume(raw_text, finish_reason, requested):
            nonlocal issues
            truncated = str(finish_reason).upper() in {"MAX_TOKENS", "LENGTH", "2"}
            try:
                payload = parse_response(raw_text)
            except (ValueError, TypeError) as exc:
                raise InvalidResponse("Model output is not valid JSON") from exc
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

        for attempt_number in range(2):
            requested = section_ids if review else [sid for sid in section_ids if sid not in accepted]
            outgoing = messages
            if issues or accepted:
                repair_package = {**package, "requested_sections": requested,
                    "response_errors": issues, "instruction": "Return only the requested sections. Other sections are already saved. If none are requested return sections: [] and notes: [] to repair the envelope only."}
                outgoing = [HumanMessage(content=instructions(node) + "\nREQUEST:\n" + dumps(repair_package))]
            try:
                size, input_tokens, _ = self.budget(outgoing, schema, timeout)
            except Oversized as exc:
                # Do not turn an oversized repair into a new whole-draft call.
                if accepted or attempt_number:
                    raise MFIRunError("The remaining response repair cannot fit its request budget", 422) from exc
                raise
            call_id, started = "llm-" + uuid.uuid4().hex[:12], time.monotonic()
            def start(v):
                sequence = v.get("light_call_sequence", 0) + 1
                v["light_call_sequence"] = sequence
                v["light_work"][key]["status"] = "running"
                v["light_work"][key]["attempts"].append({"call_id": call_id, "sequence": sequence,
                    "requested_ids": requested, "service": "mfi-drafter", "run_id": v["run_id"], "node": node,
                    "operation": "mfi.light."+node+".v1", "model": MODEL, "location": "global", "provider": "vertex_ai",
                    "configured_timeout_seconds": timeout, "configured_max_retries": 0,
                    "started_at": now(), "status": "started", "prompt_character_count": size,
                    "prompt_sha256": fingerprint([m.content for m in outgoing]), "token_usage": {"prompt_tokens": input_tokens}})
            self.ledger.change(start)
            self.notify()
            def update_attempt(**fields):
                def update(v):
                    row = next(a for a in v["light_work"][key]["attempts"] if a["call_id"] == call_id)
                    row.update(fields)
                self.ledger.change(update)
            try:
                response = self.client.generate(outgoing, schema, timeout)
                raw = extracted(response)
                metadata = getattr(response, "response_metadata", {}) or {}
                usage = getattr(response, "usage_metadata", {}) or {}
                finish_reason = metadata.get("finish_reason", "STOP")
                update_attempt(transport_status="succeeded", finish_reason=str(finish_reason),
                    response_character_count=len(raw), response_sha256=fingerprint(raw),
                    token_usage={"prompt_tokens": usage.get("input_tokens", input_tokens), "candidate_tokens": usage.get("output_tokens"),
                                 "total_tokens": usage.get("total_tokens")})
                output = consume(raw, finish_reason, requested)
                update_attempt(status="succeeded", completed_at=now(), duration_ms=int((time.monotonic()-started)*1000),
                               contract_validation_status="passed", json_parse_status="passed")
                return self.complete(key, output)
            except Exception as exc:
                update_attempt(status="failed", completed_at=now(), duration_ms=int((time.monotonic()-started)*1000),
                    error_type=type(exc).__name__, failure_code="invalid_response" if isinstance(exc, InvalidResponse) else "execution_error",
                    error_message=str(exc) if isinstance(exc, InvalidResponse) else type(exc).__name__)
                self.ledger.change(lambda v: v["light_work"][key].update(status="failed", issues=[str(exc)] if isinstance(exc, InvalidResponse) else []))
                self.notify()
                if attempt_number or not retryable(exc):
                    raise
                issues = [str(exc)] if isinstance(exc, InvalidResponse) else []
                time.sleep(1)
        raise MFIRunError(f"Attempts exhausted for {node}; generate the report again", 502)

    def complete(self, key, output):
        self.ledger.change(lambda v: v["light_work"][key].update(status="succeeded", issues=[]))
        self.notify()
        return output
