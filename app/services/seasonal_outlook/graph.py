"""The Seasonal Outlook as one LangGraph graph with an entry point per phase.

    extract:  extraction -> review -> refinement
    feedback: feedback
    report:   draft -> report_review -> redraft -> export

Each invocation runs one whole phase and ends. The analyst's review happens between two
invocations, so the graph needs no checkpointer: the analysis record keeps what the review needs.
"""
from langgraph.graph import END, START, StateGraph

from .calls import llm_request
from .engine import CHAINS
from .nodes import accept, request_for
from .nodes import export as export_node
from .science.state import SeasonalState


def build_graph(llm, recorder, *, timeout, namespace, export):
    """Compile the graph for one operation.

    llm is the operation's LLM client. Its audit, the recorder, stores each request and response and
    marks each validated stage; prepare() hands it the stage's request as the analysis record keeps it.
    export(state) builds the report files.
    """
    def stage(name):
        def run(state):
            request = request_for(name, state)
            recorder.prepare(request)

            def validate(_text, response):
                # Runs after the recorder has stored the response.
                return accept(name, state, dict(text=response.text, finish_reason=response.finish_reason),
                              f'{namespace}_{name}')
            accepted = llm.generate(llm_request(request, timeout, f'{namespace}_{name}'), validate=validate).value
            return {key: value for key, value in accepted.items() if state.get(key) != value}
        return run

    graph = StateGraph(SeasonalState)
    for stages in CHAINS.values():
        for name in stages:
            graph.add_node(name, stage(name))
        for current, following in zip(stages, stages[1:]):
            graph.add_edge(current, following)
    graph.add_node('export', lambda state: export_node.export(state, export))
    graph.add_conditional_edges(START, lambda state: state['phase'],
                                {phase: stages[0] for phase, stages in CHAINS.items()})
    graph.add_edge(CHAINS['extract'][-1], END)
    graph.add_edge(CHAINS['feedback'][-1], END)
    graph.add_edge(CHAINS['report'][-1], 'export')
    graph.add_edge('export', END)
    return graph.compile()
