"""Seerist and ReliefWeb clients: the context documents of the Market Monitor and MFI reports.

They read SEERIST_API_KEY and RELIEFWEB_APPNAME from the environment, which the entry points load.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import requests

from app.shared.countries import resolve_country

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SeeristCountryContext:
    """Provider-specific country resolution without changing report identity."""

    canonical_country: Optional[str]
    canonical_iso3: Optional[str]
    query_country: Optional[str]
    query_iso3: Optional[str]
    aoi_id: Optional[str]
    override_code: Optional[str] = None
    error: Optional[str] = None


class SeeristRetriever:
    BASE_URL = "https://app.seerist.com/hyperionapi/v1/wod"
    MAX_RETRIES = 3
    REQUEST_DELAY = 0.5
    DEFAULT_PAGE_SIZE = 50
    DEFAULT_ECON_TERMS: Sequence[str] = (
        "market",
        "prices",
        "food prices",
        "food price",
        "inflation",
        "currency",
        "exchange rate",
        "trade",
        "livelihood",
        "fuel price",
        "wage",
    )
    ISO3_TO_AOI_ID: Dict[str, str] = {
        "AFG": "AF",
        "AGO": "AO",
        "ARM": "AM",
        "BGD": "BD",
        "BEN": "BJ",
        "BFA": "BF",
        "BDI": "BI",
        "BOL": "BO",
        "CAF": "CF",
        "CIV": "CI",
        "CMR": "CM",
        "COD": "CD",
        "COG": "CG",
        "COL": "CO",
        "DJI": "DJ",
        "DZA": "DZ",
        "ECU": "EC",
        "EGY": "EG",
        "SLV": "SV",
        "LAO": "LA",
        "ETH": "ET",
        "GHA": "GH",
        "GIN": "GN",
        "GMB": "GM",
        "GNB": "GW",
        "GTM": "GT",
        "HTI": "HT",
        "HND": "HN",
        "IDN": "ID",
        "IRN": "IR",
        "IRQ": "IQ",
        "JOR": "JO",
        "KEN": "KE",
        "KGZ": "KG",
        "KHM": "KH",
        "LBN": "LB",
        "LBR": "LR",
        "LBY": "LY",
        "LKA": "LK",
        "LSO": "LS",
        "MDA": "MD",
        "MDG": "MG",
        "MLI": "ML",
        "MMR": "MM",
        "MOZ": "MZ",
        "MRT": "MR",
        "MWI": "MW",
        "NER": "NE",
        "NGA": "NG",
        "NPL": "NP",
        "PAK": "PK",
        "PHL": "PH",
        "PSE": "PS",
        "RWA": "RW",
        "SDN": "SD",
        "SEN": "SN",
        "SLE": "SL",
        "SOM": "SO",
        "SSD": "SS",
        "SWZ": "SZ",
        "SYR": "SY",
        "TCD": "TD",
        "TJK": "TJ",
        "TLS": "TL",
        "TUR": "TR",
        "TZA": "TZ",
        "UGA": "UG",
        "UKR": "UA",
        "VEN": "VE",
        "VNM": "VN",
        "YEM": "YE",
        "ZMB": "ZM",
        "ZWE": "ZW",
    }
    COUNTRY_OVERRIDES: Dict[str, tuple[str, str, str, str]] = {
        "Gaza Strip": (
            "Palestine, State of",
            "PSE",
            "PS",
            "gaza_to_palestine_aoi",
        )
    }
    UNMAPPED_CANONICAL_COUNTRIES = {"West Bank"}

    def __init__(
        self,
        verbose: bool = False,
        api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.verbose = verbose
        self.api_key = (api_key if api_key is not None else os.getenv("SEERIST_API_KEY") or "").strip()
        self.session = session or requests.Session()
        self.last_request_time = 0.0
        self.last_trace: Dict[str, Any] = {}

    def _enforce_rate_limit(self) -> None:
        elapsed = time.time() - self.last_request_time
        if elapsed < self.REQUEST_DELAY:
            time.sleep(self.REQUEST_DELAY - elapsed)
        self.last_request_time = time.time()

    @staticmethod
    def _format_datetime(date_str: str, *, end_of_day: bool = False) -> str:
        raw = (date_str or "").strip()
        if not raw:
            raise ValueError("date_str is required")
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
            if len(raw) <= 10:
                dt = dt.replace(
                    hour=23 if end_of_day else 0,
                    minute=59 if end_of_day else 0,
                    second=59 if end_of_day else 0,
                    microsecond=999000 if end_of_day else 0,
                )
        except ValueError:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            dt = dt.replace(
                hour=23 if end_of_day else 0,
                minute=59 if end_of_day else 0,
                second=59 if end_of_day else 0,
                microsecond=999000 if end_of_day else 0,
            )
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    @staticmethod
    def _escape_lucene_term(term: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(term or "").strip())
        if not cleaned:
            return ""
        cleaned = cleaned.replace('"', '\\"')
        if re.search(r"[\s:/-]", cleaned):
            return f'"{cleaned}"'
        return cleaned

    @classmethod
    def build_lucene_or_query(cls, terms: Sequence[str]) -> str:
        unique_terms: List[str] = []
        seen = set()
        for term in terms:
            escaped = cls._escape_lucene_term(term)
            if not escaped or escaped in seen:
                continue
            seen.add(escaped)
            unique_terms.append(escaped)
        if not unique_terms:
            return ""
        if len(unique_terms) == 1:
            return unique_terms[0]
        return "(" + " OR ".join(unique_terms) + ")"

    @staticmethod
    def _strip_html(html_text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", " ", str(html_text or ""))
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def _extract_text(field: Any) -> str:
        if isinstance(field, dict):
            english = field.get("en")
            if english:
                return str(english)
            for value in field.values():
                if value:
                    return str(value)
            return ""
        if field is None:
            return ""
        return str(field)

    def _resolve_country_context(self, country: str) -> SeeristCountryContext:
        try:
            canonical_country, iso3 = resolve_country(country)
        except ValueError as exc:
            return SeeristCountryContext(
                canonical_country=None,
                canonical_iso3=None,
                query_country=None,
                query_iso3=None,
                aoi_id=None,
                error=str(exc),
            )

        override = self.COUNTRY_OVERRIDES.get(canonical_country)
        if override is not None:
            query_country, query_iso3, aoi_id, override_code = override
            return SeeristCountryContext(
                canonical_country=canonical_country,
                canonical_iso3=iso3,
                query_country=query_country,
                query_iso3=query_iso3,
                aoi_id=aoi_id,
                override_code=override_code,
            )

        if canonical_country in self.UNMAPPED_CANONICAL_COUNTRIES:
            return SeeristCountryContext(
                canonical_country=canonical_country,
                canonical_iso3=iso3,
                query_country=canonical_country,
                query_iso3=iso3,
                aoi_id=None,
                error=(
                    f"Country '{canonical_country}' does not have a Seerist "
                    "aoiId mapping."
                ),
            )

        aoi_id = self.ISO3_TO_AOI_ID.get(iso3)
        if not aoi_id:
            return SeeristCountryContext(
                canonical_country=canonical_country,
                canonical_iso3=iso3,
                query_country=canonical_country,
                query_iso3=iso3,
                aoi_id=None,
                error=(
                    f"Country '{canonical_country}' ({iso3}) does not have a "
                    "Seerist aoiId mapping."
                ),
            )
        return SeeristCountryContext(
            canonical_country=canonical_country,
            canonical_iso3=iso3,
            query_country=canonical_country,
            query_iso3=iso3,
            aoi_id=aoi_id,
        )

    def _map_feature_to_document(self, feature: Dict[str, Any], idx: int) -> Dict[str, Any]:
        props = feature.get("properties", {}) if isinstance(feature, dict) else {}
        title = self._extract_text(props.get("title")) or f"Seerist report {idx + 1}"
        content = self._extract_text(props.get("sanitizedBody"))
        if not content:
            html_body = self._extract_text(props.get("body"))
            if html_body:
                content = self._strip_html(html_body)
        if not content:
            content = self._extract_text(props.get("sanitizedSummary"))
        if not content:
            content = title

        published_date = props.get("publishedDate") or props.get("@timestamp") or ""
        seerist_id = str(props.get("id") or feature.get("id") or idx)

        return {
            "doc_id": f"seerist_{seerist_id}",
            "title": title,
            "url": "",
            "source": "Seerist",
            "date": str(published_date)[:10],
            "content": content[:5000],
        }

    def _query_documents(
        self,
        *,
        search_query: str,
        start_iso: str,
        end_iso: str,
        aoi_id: str,
        max_records: int,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        query_trace: Dict[str, Any] = {
            "query": search_query,
            "aoi_id": aoi_id,
            "status_code": None,
            "max_records": max_records,
            "request_url": None,
            "num_documents": 0,
            "total_available": None,
            "samples": [],
            "error": None,
        }
        params: Dict[str, Any] = {
            "sources": "analysis",
            "start": start_iso,
            "end": end_iso,
            "pageSize": min(max_records, self.DEFAULT_PAGE_SIZE),
            "pageOffset": 0,
            "sortDirection": "desc",
            "aoiId": aoi_id,
        }
        q = (search_query or "").strip()
        if q:
            params["search"] = q

        for attempt in range(self.MAX_RETRIES):
            try:
                response = self.session.get(
                    self.BASE_URL,
                    params=params,
                    headers={"x-api-key": self.api_key},
                    timeout=30,
                )
                query_trace["status_code"] = response.status_code
                query_trace["request_url"] = getattr(response, "url", None)

                if response.status_code == 429 and attempt < self.MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue

                if response.status_code >= 400:
                    body = (response.text or "").strip()
                    query_trace["error"] = body[:2000] if body else f"HTTP {response.status_code}"
                    return [], query_trace

                try:
                    payload = response.json() if response.text.strip() else {}
                except Exception as exc:
                    query_trace["error"] = f"Failed to parse JSON response: {exc}"
                    return [], query_trace

                features = payload.get("features", []) if isinstance(payload, dict) else []
                metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}
                query_trace["total_available"] = metadata.get("total")

                documents: List[Dict[str, Any]] = []
                for idx, feature in enumerate(features if isinstance(features, list) else []):
                    document = self._map_feature_to_document(feature, idx)
                    if document.get("content"):
                        documents.append(document)

                query_trace["num_documents"] = len(documents)
                query_trace["samples"] = [
                    {
                        "title": doc.get("title", ""),
                        "date": doc.get("date", ""),
                        "doc_id": doc.get("doc_id", ""),
                    }
                    for doc in documents[:3]
                ]
                return documents, query_trace
            except requests.exceptions.RequestException as exc:
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep((attempt + 1) * 2)
                    continue
                query_trace["error"] = str(exc)
                return [], query_trace

        query_trace["error"] = "Seerist query failed"
        return [], query_trace

    def fetch(
        self,
        *,
        search_query: str,
        start_date: str,
        end_date: str,
        country: str,
        max_records: int = 50,
    ) -> List[Dict[str, Any]]:
        return self.fetch_batch(
            queries=[search_query],
            start_date=start_date,
            end_date=end_date,
            country=country,
            max_per_query=max_records,
        )

    def fetch_batch(
        self,
        *,
        queries: Sequence[str],
        start_date: str,
        end_date: str,
        country: str,
        max_per_query: int = 20,
    ) -> List[Dict[str, Any]]:
        started = time.time()
        country_context = self._resolve_country_context(country)
        unique_queries: List[str] = []
        seen_queries = set()
        for query in queries:
            normalized_query = str(query or "").strip()
            if normalized_query in seen_queries:
                continue
            seen_queries.add(normalized_query)
            unique_queries.append(normalized_query)

        trace: Dict[str, Any] = {
            "retriever": "Seerist",
            "country": country,
            "canonical_country": country_context.canonical_country,
            "country_iso3": country_context.canonical_iso3,
            "seerist_query_country": country_context.query_country,
            "seerist_query_iso3": country_context.query_iso3,
            "aoi_id": country_context.aoi_id,
            "country_override": country_context.override_code,
            "start_date": start_date,
            "end_date": end_date,
            "queries": unique_queries,
            "max_per_query": max_per_query,
            "num_documents": 0,
            "samples": [],
            "query_traces": [],
            "error": None,
            "duration_ms": None,
        }

        if not self.api_key:
            trace["error"] = "Missing SEERIST_API_KEY."
            trace["duration_ms"] = int((time.time() - started) * 1000)
            self.last_trace = trace
            if self.verbose:
                logger.info(json.dumps({"seerist_trace": trace}, ensure_ascii=False))
            return []

        if country_context.error:
            trace["error"] = country_context.error
            trace["duration_ms"] = int((time.time() - started) * 1000)
            self.last_trace = trace
            if self.verbose:
                logger.info(json.dumps({"seerist_trace": trace}, ensure_ascii=False))
            return []

        try:
            start_iso = self._format_datetime(start_date, end_of_day=False)
            end_iso = self._format_datetime(end_date, end_of_day=True)
        except ValueError as exc:
            trace["error"] = str(exc)
            trace["duration_ms"] = int((time.time() - started) * 1000)
            self.last_trace = trace
            if self.verbose:
                logger.info(json.dumps({"seerist_trace": trace}, ensure_ascii=False))
            return []

        all_documents: List[Dict[str, Any]] = []
        seen_doc_ids = set()
        query_errors: List[str] = []

        for query in unique_queries:
            self._enforce_rate_limit()
            documents, query_trace = self._query_documents(
                search_query=query,
                start_iso=start_iso,
                end_iso=end_iso,
                aoi_id=country_context.aoi_id or "",
                max_records=max_per_query,
            )
            trace["query_traces"].append(query_trace)
            if query_trace.get("error"):
                query_errors.append(str(query_trace["error"]))
            for document in documents:
                doc_id = document.get("doc_id")
                if not doc_id or doc_id in seen_doc_ids:
                    continue
                seen_doc_ids.add(doc_id)
                all_documents.append(document)

        trace["num_documents"] = len(all_documents)
        trace["samples"] = [
            {
                "title": doc.get("title", ""),
                "date": doc.get("date", ""),
                "doc_id": doc.get("doc_id", ""),
            }
            for doc in all_documents[:3]
        ]
        if not all_documents and query_errors:
            trace["error"] = "; ".join(dict.fromkeys(query_errors))
        trace["duration_ms"] = int((time.time() - started) * 1000)
        self.last_trace = trace
        if self.verbose:
            logger.info(json.dumps({"seerist_trace": trace}, ensure_ascii=False))
        return all_documents


class ReliefWebRetriever:
    BASE_URL = "https://api.reliefweb.int/v2/reports"
    DEFAULT_ECON_TERMS: Sequence[str] = (
        "market",
        "prices",
        "food prices",
        "food price",
        "inflation",
        "currency",
        "exchange rate",
        "trade",
        "livelihood*",
        "fuel price",
        "wage",
    )

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.last_trace: Dict[str, Any] = {}

    @classmethod
    def build_economy_query(cls, extra_terms: Optional[Sequence[str]] = None) -> str:
        merged_terms = list(cls.DEFAULT_ECON_TERMS)
        if extra_terms:
            merged_terms.extend(list(extra_terms))

        cleaned: List[str] = []
        for t in merged_terms:
            t = (t or "").strip()
            if not t:
                continue
            safe = t.replace('"', "\\\"")
            if re.search(r"\s", safe):
                cleaned.append(f'"{safe}"')
            else:
                cleaned.append(safe)

        if not cleaned:
            return ""
        if len(cleaned) == 1:
            return cleaned[0]
        return "(" + " OR ".join(cleaned) + ")"

    def fetch(
        self,
        country: str,
        start_date: str,
        end_date: str,
        max_records: int = 10,
        query: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        started = time.time()
        trace: Dict[str, Any] = {
            "retriever": "ReliefWeb",
            "country": country,
            "start_date": start_date,
            "end_date": end_date,
            "max_records": max_records,
            "status_code": None,
            "num_documents": 0,
            "samples": [],
            "error": None,
            "duration_ms": None,
        }

        appname = (os.getenv("RELIEFWEB_APPNAME") or "").strip()
        if not appname:
            trace["error"] = "Missing RELIEFWEB_APPNAME (ReliefWeb API requires appname in URL)"
            trace["duration_ms"] = int((time.time() - started) * 1000)
            self.last_trace = trace
            if self.verbose:
                logger.info(json.dumps({"reliefweb_trace": trace}, ensure_ascii=False))
            return []

        start_iso = f"{start_date[:10]}T00:00:00+00:00"
        end_iso = f"{end_date[:10]}T23:59:59+00:00"
        payload = {
            "filter": {
                "operator": "AND",
                "conditions": [
                    {"field": "country", "value": country},
                    {"field": "date.created", "value": {"from": start_iso, "to": end_iso}},
                ],
            },
            "limit": max_records,
            "fields": {"include": ["title", "url", "source", "date.created", "body"]},
            "sort": ["date.created:desc"],
        }
        q = (query or "").strip()
        if q:
            payload["query"] = {
                "value": q,
                "operator": "OR",
                "fields": ["title", "body"],
            }
        try:
            resp = requests.post(
                self.BASE_URL,
                params={"appname": appname},
                json=payload,
                timeout=30,
                headers={"User-Agent": f"{appname} (python requests)"},
            )

            trace["status_code"] = resp.status_code

            if resp.status_code >= 400:
                snippet = (resp.text or "").strip()
                if snippet:
                    trace["error"] = snippet[:2000]
                else:
                    trace["error"] = f"HTTP {resp.status_code}"
                trace["duration_ms"] = int((time.time() - started) * 1000)
                self.last_trace = trace
                if self.verbose:
                    logger.info(json.dumps({"reliefweb_trace": trace}, ensure_ascii=False))
                return []

            try:
                parsed = resp.json() if resp.text.strip() else {}
            except Exception as exc:
                trace["error"] = f"Failed to parse JSON response: {exc}"
                trace["duration_ms"] = int((time.time() - started) * 1000)
                self.last_trace = trace
                if self.verbose:
                    logger.info(json.dumps({"reliefweb_trace": trace}, ensure_ascii=False))
                return []

            data = parsed.get("data", []) if isinstance(parsed, dict) else []
            documents = []
            for item in data:
                fields = item.get("fields", {})
                date_created = ""
                if isinstance(fields, dict):
                    if isinstance(fields.get("date"), dict):
                        date_created = (fields.get("date") or {}).get("created", "")
                    if not date_created:
                        date_created = fields.get("date.created", "")
                body_val = fields.get("body", "") if isinstance(fields, dict) else ""
                if body_val is None:
                    body_val = ""
                if not isinstance(body_val, str):
                    body_val = json.dumps(body_val, ensure_ascii=False)
                documents.append(
                    {
                        "doc_id": f"rw_{item.get('id', '')}",
                        "title": fields.get("title", ""),
                        "url": fields.get("url", ""),
                        "source": "ReliefWeb",
                        "date": (date_created or "")[:10],
                        "content": body_val[:5000],
                    }
                )

            trace["num_documents"] = len(documents)
            trace["samples"] = [
                {"title": d.get("title", ""), "url": d.get("url", ""), "date": d.get("date", "")}
                for d in documents[:3]
            ]
            trace["duration_ms"] = int((time.time() - started) * 1000)
            self.last_trace = trace
            if self.verbose:
                logger.info(json.dumps({"reliefweb_trace": trace}, ensure_ascii=False))
            return documents
        except Exception as exc:
            trace["error"] = str(exc)
            trace["duration_ms"] = int((time.time() - started) * 1000)
            self.last_trace = trace
            if self.verbose:
                logger.info(json.dumps({"reliefweb_trace": trace}, ensure_ascii=False))
            return []
