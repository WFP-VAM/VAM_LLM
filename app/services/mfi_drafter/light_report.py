"""Section-level report blocks and the public output of the light workflow."""
from __future__ import annotations
import re
from .coverage import annex_blocks, evaluate_coverage, table_block
from .light_evidence import section_specs


def markdown_blocks(text, section_id):
    from app.shared.report_blocks import ReportBlock
    paragraph = []
    for line in [*text.strip().splitlines(), ""]:
        heading = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if not line.strip() or heading:
            if paragraph:
                yield ReportBlock(type="paragraph", text="\n".join(paragraph), meta={"section_id": section_id})
                paragraph = []
            if heading:
                yield ReportBlock(type="heading", level=min(4, len(heading[1])+1), text=heading[2], meta={"section_id": section_id})
        else:
            paragraph.append(line.strip())


def build_blocks(result):
    from app.shared.report_blocks import ReportBlock, _apply_mfi_layout_contract
    profile = result["assessment_profile"]
    narrative = result["light_narrative"]
    blocks = [ReportBlock(type="heading", level=1, text=f"Market Functionality Index — {result['country']}"),
        ReportBlock(type="paragraph", text=f"Assessment period: {result['data_collection_start']} to {result['data_collection_end']}"),
        ReportBlock(type="methodology_note", text="Scores and tables follow databridge-current. Summaries are unweighted descriptions of assessed markets and are not population-representative country estimates.")]
    for key, title in (("executive_summary", "Executive summary"), ("country_context", "Country context")):
        blocks.append(ReportBlock(type="heading", text=title, level=2))
        blocks.extend(markdown_blocks(narrative["summary"][key], key))
    rows = [{"dimension": d["dimension"], "mean": f"{d['statistics']['mean']:.2f}",
             "markets": d["statistics"]["denominator"]} for d in profile["dimensions"]]
    blocks.append(table_block("Dimension scores across assessed markets", rows,
        [("dimension", "Dimension"), ("mean", "Mean /10"), ("markets", "Markets")]))
    used = set()
    def figure(figure_id):
        if figure_id in result.get("visualizations", {}) and figure_id not in used:
            used.add(figure_id)
            if figure_id == "geographic_map":
                from .map_rendering import MAP_WIDTH
                meta = result.get('figure_metadata', {}).get(figure_id, {})
                coverage = (f" Coordinates available for {meta['located_market_count']}/{meta['total_market_count']} assessed markets."
                            if 'located_market_count' in meta else '')
                caption = ('Stored assessed-market MFI scores in the assessed area. Colours show scores on the 0–10 scale; '
                           'numbered callouts identify selected markets in selection order and match the side legend.' + coverage)
                caption += ' ' + ' '.join(v for v in meta.get('limitations', []) if not v.startswith('Coordinates available for'))
                blocks.append(ReportBlock(type='figure', figure_id=figure_id, width=MAP_WIDTH,
                    caption=caption.strip(), alt_text=caption.strip()))
                return
            blocks.append(ReportBlock(type="figure", figure_id=figure_id,
                caption=figure_id.replace("_", " "), alt_text="Chart computed from the assessed market data"))
    for key in ("mfi_radar", "market_score_ranking", "geographic_map", "overview_table"):
        figure(key)
    for family, title in (("dimensions", "MFI dimensions"), ("markets", "Selected markets")):
        blocks.append(ReportBlock(type="heading", text=title, level=2))
        for spec in section_specs(profile, family):
            sid = spec["section_id"]
            if not narrative[family].get(sid, "").strip():
                raise ValueError(f"Missing final section: {sid}")
            blocks.append(ReportBlock(type="heading", text=spec["title"], level=3, meta={"section_id": sid}))
            blocks.extend(markdown_blocks(narrative[family][sid], sid))
            if family == "dimensions":
                slug = re.sub(r"[^a-z0-9_]+", "_", sid.lower().replace(" ", "_").replace("&", "and")).strip("_")
                for key in (f"dim_{slug}_bars", f"priority_{slug}_subsections", f"priority_{slug}_drivers", f"priority_{slug}_items"):
                    figure(key)
    blocks.extend(annex_blocks(result))
    for warning in dict.fromkeys([*(result.get("warnings") or []), *narrative.get("notes", [])]):
        blocks.append(ReportBlock(type="limitation_box", text=warning))
    if result.get("document_references"):
        blocks.append(ReportBlock(type="references", references=result["document_references"]))
    coverage = evaluate_coverage(profile, blocks)
    if not coverage["complete"]:
        raise ValueError("The analytical annex is missing required evidence tables")
    blocks = _apply_mfi_layout_contract(blocks, country=result["country"], methodology_version="databridge-current")
    return [b.model_dump(mode="json") for b in blocks], coverage


def output_aliases(result):
    """Coarse legacy display aliases, never fabricated claim verification records."""
    narrative = result["light_narrative"]
    return {"executive_summary": narrative["summary"]["executive_summary"],
        "country_context": narrative["summary"]["country_context"],
        "dimension_findings": {s["title"]: {"key_findings": narrative["dimensions"][s["section_id"]],
                               "score_interpretation": "", "recommendations": ""} for s in section_specs(result["assessment_profile"], "dimensions")},
        "market_recommendations": {s["title"]: {"narrative": narrative["markets"][s["section_id"]]} for s in section_specs(result["assessment_profile"], "markets")}}


def public_output(result):
    from .compatibility import canonical_and_legacy_response_fields
    allowed = ("run_id", "workflow_revision", "response_contract_bundle", "effective_contract", "country", "data_collection_start", "data_collection_end",
        "analysis_schema_version", "narrative_schema_version", "methodology_version", "score_authority", "release_control",
        "survey_metadata", "excluded_market_records", "methodology_warnings", "light_narrative", "review_reports", "review_status",
        "generation_diagnostics", "llm_diagnostics", "document_references", "report_blocks", "visualizations", "figure_metadata",
        "warnings", "llm_calls", "success", "coverage", "context_status")
    return {**{key:result[key] for key in allowed if key in result},
            **canonical_and_legacy_response_fields(result), **output_aliases(result)}
