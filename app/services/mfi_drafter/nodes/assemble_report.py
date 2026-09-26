"""Node assemble_report: the run's result, with its report blocks and coverage."""
from __future__ import annotations
from ..sections import texts


def assemble(s):
    from ..report import build_blocks, output_aliases
    result = {**s["base"], **s["context"], **s["figures"], "success": True,
        "light_narrative": {"dimensions": texts(s["final_dimensions"]), "markets": texts(s["final_markets"]),
            "summary": texts(s["summary"]), "notes": list(dict.fromkeys([*s["final_dimensions"]["notes"], *s["final_markets"]["notes"], *s["summary"]["notes"]]))},
        "review_reports": {"dimensions": s["review_dimensions"], "markets": s["review_markets"]}, "review_status": "completed"}
    result["report_blocks"], result["coverage"] = build_blocks(result)
    result.update(output_aliases(result))
    return {"report": result}
