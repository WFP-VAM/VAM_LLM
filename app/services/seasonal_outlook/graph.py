"""The Seasonal Outlook as one LangGraph graph with an entry point per phase.

    extract:  extraction -> review -> refinement
    feedback: feedback
    report:   draft -> report_review -> redraft -> export

Each invocation runs one whole phase and ends. The analyst's review happens between two
invocations, so the graph needs no checkpointer: the analysis record keeps what the review needs.
"""
from langgraph.graph import END, START, StateGraph

from .engine import CHAINS, accept, request_for
from .science.state import SeasonalState


def build_graph(provider, recorder, *, timeout, namespace, export):
    """Compile the graph for one operation.

    provider.complete(request, timeout) calls the model once. The recorder stores each request and
    response and marks each validated stage. export(state) builds the report files.
    """
    def stage(name):
        def run(state):
            request = request_for(name, state)
            recorder.requested(name, request)
            response = provider.complete(request, timeout)
            recorder.responded(response)  # The response is stored before it is validated.
            accepted = accept(name, state, response, f'{namespace}_{name}')
            recorder.validated(name)
            return {key: value for key, value in accepted.items() if state.get(key) != value}
        return run

    graph = StateGraph(SeasonalState)
    for stages in CHAINS.values():
        for name in stages:
            graph.add_node(name, stage(name))
        for current, following in zip(stages, stages[1:]):
            graph.add_edge(current, following)
    graph.add_node('export', lambda state: {'artifacts': export(state)})
    graph.add_conditional_edges(START, lambda state: state['phase'],
                                {phase: stages[0] for phase, stages in CHAINS.items()})
    graph.add_edge(CHAINS['extract'][-1], END)
    graph.add_edge(CHAINS['feedback'][-1], END)
    graph.add_edge(CHAINS['report'][-1], 'export')
    graph.add_edge('export', END)
    return graph.compile()
