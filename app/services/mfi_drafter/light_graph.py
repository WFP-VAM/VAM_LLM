"""Eleven LangGraph nodes; two draft/review/correction branches and a final synthesis."""
from __future__ import annotations
from typing import TypedDict
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, Future
from langgraph.graph import StateGraph, START, END
from .light_contracts import WORKFLOW, NODES
from .light_evidence import evidence, section_specs, source_map
from .light_runtime import ModelRuntime, Oversized, public_diagnostics


class State(TypedDict, total=False):
    base: dict
    context: dict
    figures: dict
    draft_dimensions: dict
    draft_markets: dict
    review_dimensions: dict
    review_markets: dict
    final_dimensions: dict
    final_markets: dict
    summary: dict
    report: dict


def prepare_analysis(inputs):
    from .analysis import build_assessment_profile
    from .mock_data import generate_mock_mfi_data
    loaded = inputs.get("csv_data")
    if loaded is None:
        loaded = generate_mock_mfi_data(inputs["country"], inputs["markets"], inputs["data_collection_start"], inputs["data_collection_end"])
    profile = build_assessment_profile(loaded["markets_data"], loaded["metric_summaries"], loaded).model_dump(mode="json")
    return {**{k: inputs[k] for k in ("country", "data_collection_start", "data_collection_end", "run_id")},
        **{k: deepcopy(loaded.get(k)) for k in ("markets_data", "metric_summaries", "survey_metadata", "score_authority", "methodology_version",
            "excluded_market_records", "methodology_warnings", "warnings")},
        "assessment_profile": profile, "workflow_revision": WORKFLOW, "analysis_schema_version": "2.1",
        "narrative_schema_version": "3.0", "mean_mfi_across_assessed_markets": profile["mean_mfi_across_assessed_markets"],
        "release_control": inputs["release_control"]}


def retrieve_context(base):
    from .context import retrieve_context_documents
    from .context_status import resolve_context_status
    try:
        retrieved = retrieve_context_documents(base)
    except Exception as exc:
        # Context has always been optional. Keep its failure distinct from analysis.
        retrieved = {"contextual_documents": [], "retriever_traces": [{"retriever": "context", "error": type(exc).__name__}],
                     "context_status": resolve_context_status(retriever_statuses={"ReliefWeb":"failed", "Seerist":"failed"}, documents=[]).model_dump()}
    sources = source_map(retrieved.get("contextual_documents", []))
    context_status = dict(retrieved.get("context_status") or {})
    for legacy_field in ("statements_classified", "final_accepted_statements", "extraction_mode", "classification_outcome", "unresolved_statement_count"):
        context_status.pop(legacy_field, None)
    context_status.update(status="available" if sources else context_status.get("status", "unavailable"),
                          classification_mode="integrated_drafting_and_review", total_documents=len(sources))
    return {**{k: retrieved.get(k, []) for k in ("contextual_documents", "retriever_traces", "seerist_documents", "reliefweb_documents")},
        "context_status": context_status, "sources": sources,
        "context_limitation": None if sources else "No usable external context was retrieved; interpretation relies on the MFI assessment.",
        "document_references": [{**{k:v for k,v in source.items() if k != "content"},
            "original_document_id": source.get("doc_id"), "doc_id": key, "source_id": key} for key,source in sources.items()]}


def render_figures(base, execution):
    from .render_worker import RenderWorker, figure_jobs
    worker = RenderWorker()
    images, metadata = {}, {}
    try:
        for job in figure_jobs(base):
            result = execution.execute_once("figure:" + job["figure_id"], job, lambda job=job: worker.run(job), kind="figure")
            images[job["figure_id"]], metadata[job["figure_id"]] = result["image"], result["metadata"]
    finally:
        worker.close()
    return {"visualizations": images, "figure_metadata": metadata}


def texts(response):
    return {s["section_id"]: s["text_markdown"] for s in response["sections"]}


def build_graph(execution, *, client=None, on_step=None, trace_sink=None):
    import threading
    callback_lock = threading.RLock()
    def notify(name=None, value=None):
        with callback_lock:
            diag = public_diagnostics(execution.store.read(execution.run_id))
            if trace_sink:
                trace_sink(diag["llm_diagnostics"])
            if on_step:
                on_step(name or "model_call", {**(value or {}), "workflow_revision": WORKFLOW,
                    "generation_diagnostics": {k:v for k,v in diag.items() if k != "llm_diagnostics"},
                    "llm_diagnostics": diag["llm_diagnostics"]})
    runtime = ModelRuntime(execution, client, notify=notify)

    # LangGraph supersteps normally wait for all siblings. Scheduling owned
    # outputs as futures lets a correction follow its own review immediately,
    # and lets generation overlap charts. Futures never enter checkpoints.
    pool = None
    owned = dict(zip(NODES, ("base", "context", "figures", "draft_dimensions", "draft_markets",
        "review_dimensions", "review_markets", "final_dimensions", "final_markets", "summary", "report")))
    requirements = {
        "prepare_analysis": ("base",), "context_retrieval": ("base",), "charts": ("base",),
        "draft_dimensions": ("base", "context"), "draft_markets": ("base", "context"),
        "review_dimensions": ("base", "context", "draft_dimensions", "draft_markets"),
        "review_markets": ("base", "context", "draft_dimensions", "draft_markets"),
        "correct_dimensions": ("base", "context", "draft_dimensions", "draft_markets", "review_dimensions"),
        "correct_markets": ("base", "context", "draft_dimensions", "draft_markets", "review_markets"),
        "executive_summary": ("base", "context", "final_dimensions", "final_markets"),
        "assemble_report": ("base", "context", "figures", "final_dimensions", "final_markets", "review_dimensions", "review_markets", "summary"),
    }

    def stage(name, function, dependencies):
        def execute(state):
            state = {key: (state[key].result() if isinstance(state[key], Future) else state[key]) for key in requirements[name]}
            deps = dependencies(state)
            before = execution.store.read(execution.run_id)
            def start(v):
                v.setdefault("light_phases", {})[name] = {"status": "running"}
            execution.change(start)
            notify(name)
            try:
                value = execution.execute_once("light:"+name, deps, lambda: function(state), kind="phase")
                from .reliable_contracts import fingerprint
                dep = fingerprint([before["input_fingerprint"], before["contract_bundle"], deps, None])
                reused = any(r["task_id"] == "light:"+name and r["input_fingerprint"] == dep and r["status"] == "succeeded" for r in before["tasks"].values())
                execution.change(lambda v: v["light_phases"][name].update(status="succeeded", reused=reused))
                if name == "prepare_analysis":
                    execution.snapshot(value["base"])
                notify(name, value.get("context", value.get("base", {})))
                return value[owned[name]]
            except Exception:
                execution.change(lambda v: v["light_phases"][name].update(status="failed"))
                notify(name)
                raise
        def wrapped(state):
            return {owned[name]: pool.submit(execute, state)}
        return wrapped

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

    def generate_family(state, family, kind):
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
            execution.change(lambda v: v.setdefault("light_review_outcomes", {}).update({family: {"needs_revision": result["needs_revision"]}}))
            return result
        return {"sections": [s for o in outputs for s in o["sections"]], "notes": [n for o in outputs for n in o["notes"]]}

    graph = StateGraph(State)
    graph.add_node("prepare_analysis", stage("prepare_analysis", lambda s: {"base": prepare_analysis(s["base"])}, lambda s: s["base"]))
    graph.add_node("context_retrieval", stage("context_retrieval", lambda s: {"context": retrieve_context(s["base"])}, lambda s: {k:s["base"][k] for k in ("country", "data_collection_start", "data_collection_end")}))
    from .map_basemap import chart_dependencies
    graph.add_node("charts", stage("charts", lambda s: {"figures": render_figures(s["base"], execution)},
        lambda s: chart_dependencies(s["base"])))
    for family in ("dimensions", "markets"):
        draft, review, correct = "draft_"+family, "review_"+family, "correct_"+family
        graph.add_node(draft, stage(draft, lambda s, f=family, n=draft: {n: generate_family(s,f,n)}, lambda s: {"base":s["base"], "context":s["context"]}))
        graph.add_node(review, stage(review, lambda s, f=family, n=review: {n: generate_family(s,f,n)},
            lambda s: {k:s[k] for k in ("base", "context", "draft_dimensions", "draft_markets")}))
        def correction(s, f=family, n=correct):
            result = generate_family(s,f,n) if s["review_"+f]["needs_revision"] else s["draft_"+f]
            execution.change(lambda v: v.setdefault("light_review_outcomes", {}).setdefault(f, {}).update(
                correction="completed" if s["review_"+f]["needs_revision"] else "skipped"))
            return {"final_"+f: result}
        graph.add_node(correct, stage(correct, correction, lambda s, f=family: {k:s[k] for k in ("base", "context", "draft_dimensions", "draft_markets", "review_"+f)}))
        graph.add_edge("context_retrieval", draft)
        graph.add_edge(review, correct)
    for family in ("dimensions", "markets"):
        graph.add_edge(["draft_dimensions", "draft_markets"], "review_"+family)

    def synthesis(s):
        base = {**s["base"], **s["context"]}
        package = {"requested_sections": ["executive_summary", "country_context"], "EVIDENCE": evidence(base, "summary", []),
            "FINAL_DIMENSIONS": texts(s["final_dimensions"]), "FINAL_MARKETS": texts(s["final_markets"]),
            "FINAL_NOTES": [*s["final_dimensions"]["notes"], *s["final_markets"]["notes"]]}
        return {"summary": runtime.invoke("executive_summary", "executive_summary", package, ["executive_summary", "country_context"])}
    graph.add_node("executive_summary", stage("executive_summary", synthesis,
        lambda s: {k:s[k] for k in ("base", "context", "final_dimensions", "final_markets")}))

    def assemble(s):
        from .light_report import build_blocks, output_aliases
        result = {**s["base"], **s["context"], **s["figures"], "success": True,
            "light_narrative": {"dimensions": texts(s["final_dimensions"]), "markets": texts(s["final_markets"]),
                "summary": texts(s["summary"]), "notes": list(dict.fromkeys([*s["final_dimensions"]["notes"], *s["final_markets"]["notes"], *s["summary"]["notes"]]))},
            "review_reports": {"dimensions": s["review_dimensions"], "markets": s["review_markets"]}, "review_status": "completed"}
        result["report_blocks"], result["coverage"] = build_blocks(result)
        result.update(output_aliases(result))
        return {"report": result}
    graph.add_node("assemble_report", stage("assemble_report", assemble,
        lambda s: {k:s[k] for k in ("base", "context", "figures", "final_dimensions", "final_markets", "review_dimensions", "review_markets", "summary")}))
    graph.add_edge(START, "prepare_analysis")
    graph.add_edge("prepare_analysis", "context_retrieval")
    graph.add_edge("prepare_analysis", "charts")
    graph.add_edge(["correct_dimensions", "correct_markets"], "executive_summary")
    graph.add_edge(["executive_summary", "charts"], "assemble_report")
    graph.add_edge("assemble_report", END)
    compiled = graph.compile()

    class ExecutionGraph:
        def get_graph(self, **kwargs):
            return compiled.get_graph(**kwargs)

        def invoke(self, state, config=None):
            nonlocal pool
            # At most eleven jobs, with an acyclic dependency order. Independent
            # successful branches are committed even when another branch fails.
            with ThreadPoolExecutor(max_workers=len(NODES), thread_name_prefix="mfi-light") as executor:
                pool = executor
                scheduled = compiled.invoke(state, config=config)
                return {"report": scheduled["report"].result()}

    return ExecutionGraph()
