"""What the drafting nodes share: the section texts of a response, the package a request sends, and the
drafting of one section family, split in halves while a request exceeds its budget."""
from __future__ import annotations
from .evidence import evidence, section_specs
from .runtime import Oversized


def texts(response):
    return {s["section_id"]: s["text_markdown"] for s in response["sections"]}


def package_for(state, family, ids, kind):
    base = {**state["base"], **state["context"]}
    specs = [s for s in section_specs(base["assessment_profile"], family) if s["section_id"] in ids]
    payload = {"requested_sections": specs, "EVIDENCE": evidence(base, family, ids)}
    if kind.startswith(("review", "correct")):
        draft = state["draft_"+family]
        payload["ORIGINAL_DRAFT"] = {k:v for k,v in texts(draft).items() if k in ids}
        payload["ORIGINAL_DRAFT_NOTES"] = draft["notes"]
        other = "markets" if family == "dimensions" else "dimensions"
        payload["OTHER_DRAFT_READ_ONLY"] = texts(state["draft_"+other])
    if kind.startswith("correct"):
        payload["REVIEW_REPORT"] = state["review_"+family]
    return payload


def generate_family(runtime, ledger, state, family, kind):
    ids = [s["section_id"] for s in section_specs(state["base"]["assessment_profile"], family)]
    review = kind.startswith("review")
    def run(group):
        package = package_for(state, family, group, kind)
        try:
            from .reliable_contracts import fingerprint
            return [runtime.invoke(kind, kind+":"+fingerprint(group)[:16], package, group, review=review)]
        except Oversized:
            if len(group) <= 1:
                raise
            middle = len(group)//2
            return [*run(group[:middle]), *run(group[middle:])]
    if not ids:
        return {"needs_revision": False, "review_markdown": "No selected markets."} if review else {"sections": [], "notes": []}
    outputs = run(ids)
    if review:
        result = {"needs_revision": any(o["needs_revision"] for o in outputs),
                  "review_markdown": "\n\n".join(o["review_markdown"] for o in outputs)}
        ledger.change(lambda v: v.setdefault("light_review_outcomes", {}).update({family: {"needs_revision": result["needs_revision"]}}))
        return result
    return {"sections": [s for o in outputs for s in o["sections"]], "notes": [n for o in outputs for n in o["notes"]]}
