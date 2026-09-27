"""Context documents for a report, as the Market Monitor and MFI drafters gather them.

gather_context fetches ReliefWeb reports and Seerist analyses for a period, then merges them: a document whose URL
(or, without one, its id) was already seen is dropped, and a document without content falls back to its title.
live_document_sections turns what a run gathered into its live previews, one section per source.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence

from app.shared.runs.live_outputs import build_document_live_output, create_document_previews_with_artifacts

REFERENCE_FIELDS = ("doc_id", "source", "title", "url", "date")


@dataclass(frozen=True)
class ContextDocuments:
    """What one retrieval found: each source's documents, the merged list and each retriever's trace."""

    reliefweb: List[Dict[str, Any]]
    seerist: List[Dict[str, Any]]
    documents: List[Dict[str, Any]]
    source_traces: Mapping[str, Dict[str, Any]]

    @property
    def traces(self) -> List[Dict[str, Any]]:
        """The retrievers' traces, ReliefWeb first; a retriever that left none is skipped."""
        return [trace for trace in self.source_traces.values() if trace]

    def references(self) -> List[Dict[str, Any]]:
        return [{key: document.get(key) for key in REFERENCE_FIELDS} for document in self.documents]

    def counts(self) -> Dict[str, int]:
        counts = Counter(document.get("source", "Unknown") for document in self.documents)
        return {"Seerist": int(counts.get("Seerist", 0)), "ReliefWeb": int(counts.get("ReliefWeb", 0)),
                "total": len(self.documents)}

    def error(self, source: str) -> Optional[Any]:
        return self.source_traces[source].get("error")

    def status(self, source: str) -> str:
        documents = self.reliefweb if source == "ReliefWeb" else self.seerist
        return "failed" if self.error(source) else "completed" if documents else "no_results"


def gather_context(
    reliefweb: Any,
    seerist: Any,
    *,
    country: str,
    start_date: str,
    end_date: str,
    reliefweb_terms: Sequence[str],
    seerist_terms: Sequence[str],
    seerist_focus: Sequence[str],
    limit: int,
) -> ContextDocuments:
    """Fetch up to `limit` documents from each source and merge them.

    ReliefWeb is searched for the economy terms plus reliefweb_terms. Seerist runs three queries, each up to `limit`
    documents: the economy terms plus seerist_terms, the seerist_focus terms, and no search terms at all.
    """
    reliefweb_documents = reliefweb.fetch(
        country=country,
        start_date=start_date,
        end_date=end_date,
        max_records=limit,
        query=reliefweb.build_economy_query(extra_terms=reliefweb_terms),
    )
    queries = [
        seerist.build_lucene_or_query(list(seerist.DEFAULT_ECON_TERMS) + list(seerist_terms)),
        seerist.build_lucene_or_query(list(seerist_focus)),
        "",
    ]
    seerist_documents = seerist.fetch_batch(
        queries=queries,
        start_date=start_date,
        end_date=end_date,
        country=country,
        max_per_query=limit,
    )[:limit]

    documents: List[Dict[str, Any]] = []
    seen = set()
    for document in [*reliefweb_documents, *seerist_documents]:
        key = (document.get("url") or "").strip() or document.get("doc_id")
        if not key or key in seen:
            continue
        seen.add(key)
        if not document.get("content"):
            document["content"] = document.get("title", "")
        documents.append(document)
    return ContextDocuments(
        reliefweb=reliefweb_documents,
        seerist=seerist_documents,
        documents=documents,
        source_traces={
            "ReliefWeb": getattr(reliefweb, "last_trace", None) or {},
            "Seerist": getattr(seerist, "last_trace", None) or {},
        },
    )


@dataclass(frozen=True)
class SourceLabels:
    """The words of the live document sections; {count}, {source} and {error} are filled in."""

    seerist_title: str = "Seerist Documents"
    reliefweb_title: str = "ReliefWeb Documents"
    summary: str = "{count} {source} documents retrieved."
    unavailable: str = "{source} retrieval unavailable: {error}"


def _trace_error(traces: List[Dict[str, Any]], retriever_name: str) -> Optional[str]:
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        if str(trace.get("retriever") or "") != retriever_name:
            continue
        error = trace.get("error")
        if error:
            return str(error)
    return None


def live_document_sections(
    state: Mapping[str, Any],
    *,
    run_id: str,
    service_slug: str,
    labels: SourceLabels = SourceLabels(),
) -> Dict[str, Any]:
    """The live previews of the documents a context step put in the state, with their downloads, by source."""
    traces = state.get("retriever_traces")
    traces = traces if isinstance(traces, list) else []
    sections: Dict[str, Any] = {}
    for key, source, title in (
        ("seerist", "Seerist", labels.seerist_title),
        ("reliefweb", "ReliefWeb", labels.reliefweb_title),
    ):
        documents = state.get(f"{key}_documents") or []
        if not isinstance(documents, list):
            continue
        error = _trace_error(traces, source)
        previews = create_document_previews_with_artifacts(
            run_id=run_id,
            service_slug=service_slug,
            source_slug=key,
            documents=documents,
        )
        sections[key] = build_document_live_output(
            title=title,
            summary=(
                labels.unavailable.format(source=source, error=error)
                if error
                else labels.summary.format(count=len(documents), source=source)
            ),
            documents=previews,
            status="failed" if error else "completed",
        )
    return sections
