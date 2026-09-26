"""Eleven LangGraph nodes; two draft/review/correction branches and a final synthesis."""
from __future__ import annotations
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
# Aliases: build_graph names its per-family nodes draft, review and correct.
from .nodes import assemble_report as assemble_node, charts as charts_node, context_retrieval as context_node
from .nodes import correct as correct_node, draft as draft_node, executive_summary as summary_node
from .nodes import prepare_analysis as analysis_node, review as review_node
from .runtime import ModelRuntime


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


def build_graph(ledger, llm, notify):
    """The graph of one run: `llm` makes its model calls and `notify(name, value)` reports each phase."""
    runtime = ModelRuntime(ledger, llm)

    def stage(name, function):
        def execute(state):
            ledger.change(lambda v: v.setdefault("light_phases", {}).update({name: {"status": "running"}}))
            notify(name)
            try:
                value = function(state)
            except Exception:
                ledger.change(lambda v: v["light_phases"][name].update(status="failed"))
                notify(name)
                raise
            ledger.change(lambda v: v["light_phases"][name].update(status="succeeded"))
            notify(name, value.get("context", value.get("base", {})))
            return value
        return execute

    # Nodes are called through their module at run time, so a test can replace a node function there.
    # The node functions that take the run's runtime are only called here, never registered directly:
    # LangGraph would fill a parameter named runtime itself.
    graph = StateGraph(State)
    graph.add_node("prepare_analysis", stage("prepare_analysis", lambda s: {"base": analysis_node.prepare_analysis(s["base"])}))
    graph.add_node("context_retrieval", stage("context_retrieval", lambda s: {"context": context_node.retrieve_context(s["base"])}))
    graph.add_node("charts", stage("charts", lambda s: {"figures": charts_node.render_figures(s["base"])}))
    for family in ("dimensions", "markets"):
        draft, review, correct = "draft_"+family, "review_"+family, "correct_"+family
        graph.add_node(draft, stage(draft, lambda s, f=family, n=draft: draft_node.draft_family(runtime, ledger, s, f, n)))
        graph.add_node(review, stage(review, lambda s, f=family, n=review: review_node.review_family(runtime, ledger, s, f, n)))
        graph.add_node(correct, stage(correct, lambda s, f=family, n=correct: correct_node.correction(runtime, ledger, s, f, n)))
        graph.add_edge("context_retrieval", draft)
        graph.add_edge(review, correct)
    for family in ("dimensions", "markets"):
        graph.add_edge(["draft_dimensions", "draft_markets"], "review_"+family)
    graph.add_node("executive_summary", stage("executive_summary", lambda s: summary_node.synthesis(runtime, s)))
    graph.add_node("assemble_report", stage("assemble_report", lambda s: assemble_node.assemble(s)))
    graph.add_edge(START, "prepare_analysis")
    graph.add_edge("prepare_analysis", "context_retrieval")
    # Charts render in the same step as the two drafts, so drafting never waits for them.
    graph.add_edge("context_retrieval", "charts")
    graph.add_edge(["correct_dimensions", "correct_markets"], "executive_summary")
    graph.add_edge(["executive_summary", "charts"], "assemble_report")
    graph.add_edge("assemble_report", END)
    return graph.compile()
