"""The Seasonal Outlook's stages, one module each, and the dispatch by stage name that graph.py uses.

Each LLM stage module has request(state), the request as the analysis record keeps it, and
accept(state, response, namespace), the state after the stage's validated reply. export is the report
phase's last node.
"""
from . import draft, extraction, feedback, redraft, refinement, report_review, review

STAGES = {'extraction': extraction, 'review': review, 'refinement': refinement, 'feedback': feedback,
          'draft': draft, 'report_review': report_review, 'redraft': redraft}


def request_for(stage, state):
    return STAGES[stage].request(state)


def accept(stage, state, response, namespace):
    return STAGES[stage].accept(state, response, namespace)
