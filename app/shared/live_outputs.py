from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence

from app.shared.async_runs import add_run_artifact

_MAX_EXCERPT_CHARS = 320
_MAX_PREVIEW_ROWS = 50
_MAX_PREVIEW_COLUMNS = 12


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_preview_table(
    rows: Sequence[Dict[str, Any]],
    *,
    max_rows: int = _MAX_PREVIEW_ROWS,
    max_columns: int = _MAX_PREVIEW_COLUMNS,
) -> Dict[str, Any]:
    ordered_columns: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            key_str = str(key)
            if key_str not in ordered_columns:
                ordered_columns.append(key_str)
    columns = ordered_columns[:max_columns]

    rows_preview: List[Dict[str, Any]] = []
    for row in list(rows)[:max_rows]:
        if not isinstance(row, dict):
            continue
        rows_preview.append({column: _json_safe(row.get(column)) for column in columns})

    return {
        "columns": columns,
        "rows_preview": rows_preview,
        "count": len(list(rows)),
    }


def build_databridges_live_output(
    *,
    title: str,
    summary: str,
    rows: Sequence[Dict[str, Any]],
    download_artifacts: Sequence[Dict[str, Any]],
    status: str = "completed",
) -> Dict[str, Any]:
    preview = build_preview_table(rows)
    return {
        "kind": "table",
        "status": status,
        "title": title,
        "summary": summary,
        "count": preview["count"],
        "columns": preview["columns"],
        "rows_preview": preview["rows_preview"],
        "download_artifacts": list(download_artifacts),
        "updated_at": utc_timestamp(),
    }


def build_document_live_output(
    *,
    title: str,
    summary: str,
    documents: Sequence[Dict[str, Any]],
    status: str = "completed",
) -> Dict[str, Any]:
    return {
        "kind": "documents",
        "status": status,
        "title": title,
        "summary": summary,
        "count": len(list(documents)),
        "documents": list(documents),
        "updated_at": utc_timestamp(),
    }


def create_databridges_artifacts(
    *,
    run_id: str,
    service_slug: str,
    label_prefix: str,
    file_stem: str,
    rows: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    json_bytes = json.dumps(_json_safe(list(rows)), ensure_ascii=False, indent=2).encode("utf-8")
    csv_bytes = rows_to_csv_bytes(rows)
    return [
        _register_artifact(
            run_id=run_id,
            service_slug=service_slug,
            label=f"{label_prefix} JSON",
            mime_type="application/json",
            file_name=f"{safe_file_stem(file_stem)}.json",
            content=json_bytes,
        ),
        _register_artifact(
            run_id=run_id,
            service_slug=service_slug,
            label=f"{label_prefix} CSV",
            mime_type="text/csv",
            file_name=f"{safe_file_stem(file_stem)}.csv",
            content=csv_bytes,
        ),
    ]


def create_document_previews_with_artifacts(
    *,
    run_id: str,
    service_slug: str,
    source_slug: str,
    documents: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    previews: List[Dict[str, Any]] = []
    for idx, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            continue
        doc_id = str(document.get("doc_id") or f"{source_slug}_{idx}")
        safe_stem = safe_file_stem(f"{source_slug}-{doc_id}")
        json_artifact = _register_artifact(
            run_id=run_id,
            service_slug=service_slug,
            label=f"{doc_id} JSON",
            mime_type="application/json",
            file_name=f"{safe_stem}.json",
            content=json.dumps(_json_safe(document), ensure_ascii=False, indent=2).encode("utf-8"),
        )
        text_artifact = _register_artifact(
            run_id=run_id,
            service_slug=service_slug,
            label=f"{doc_id} TXT",
            mime_type="text/plain",
            file_name=f"{safe_stem}.txt",
            content=document_to_text(document).encode("utf-8"),
        )
        previews.append(
            {
                "doc_id": doc_id,
                "title": str(document.get("title") or doc_id),
                "date": str(document.get("date") or ""),
                "url": str(document.get("url") or ""),
                "source": str(document.get("source") or ""),
                "excerpt": excerpt_text(document.get("content") or document.get("title") or ""),
                "download_json_path": json_artifact["download_path"],
                "download_json_file_name": json_artifact["file_name"],
                "download_text_path": text_artifact["download_path"],
                "download_text_file_name": text_artifact["file_name"],
            }
        )
    return previews


def document_to_text(document: Dict[str, Any]) -> str:
    lines = [
        f"Title: {document.get('title') or ''}",
        f"Source: {document.get('source') or ''}",
        f"Date: {document.get('date') or ''}",
        f"URL: {document.get('url') or ''}",
        "",
        str(document.get("content") or ""),
    ]
    return "\n".join(lines).strip() + "\n"


def excerpt_text(text: Any, *, max_chars: int = _MAX_EXCERPT_CHARS) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "…"


def rows_to_csv_bytes(rows: Sequence[Dict[str, Any]]) -> bytes:
    output = io.StringIO()
    ordered_columns: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row.keys():
            key_str = str(key)
            if key_str not in ordered_columns:
                ordered_columns.append(key_str)

    writer = csv.DictWriter(output, fieldnames=ordered_columns)
    writer.writeheader()
    for row in rows:
        if not isinstance(row, dict):
            continue
        writer.writerow({key: _json_safe(row.get(key)) for key in ordered_columns})
    return output.getvalue().encode("utf-8")


def safe_file_stem(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    cleaned = cleaned.strip(".-_")
    return cleaned or "download"


def _register_artifact(
    *,
    run_id: str,
    service_slug: str,
    label: str,
    mime_type: str,
    file_name: str,
    content: bytes,
) -> Dict[str, Any]:
    artifact_id = f"artifact_{safe_file_stem(label)}_{datetime.now(timezone.utc).strftime('%H%M%S%f')}"
    download_path = f"/{service_slug}/artifacts/{run_id}/{artifact_id}"
    return add_run_artifact(
        run_id,
        artifact_id=artifact_id,
        label=label,
        mime_type=mime_type,
        file_name=file_name,
        download_path=download_path,
        content=content,
    )


def _json_safe(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))
    except Exception:
        return str(value)


def _json_default(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_default(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_default(item) for key, item in value.items()}

    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:
            pass

    try:
        import numpy as np  # type: ignore

        if isinstance(value, np.generic):
            return value.item()
    except Exception:
        pass

    return str(value)
