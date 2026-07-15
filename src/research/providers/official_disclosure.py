# -*- coding: utf-8 -*-
"""Official A-share disclosure discovery and archival with a strict URL allowlist."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urljoin, urlparse

import requests


DEFAULT_ALLOWED_DISCLOSURE_HOSTS = frozenset(
    {
        "www.cninfo.com.cn",
        "static.cninfo.com.cn",
        "www.sse.com.cn",
        "static.sse.com.cn",
        "www.szse.cn",
        "disc.static.szse.cn",
        "www.bse.cn",
        "static.bse.cn",
    }
)


class OfficialDisclosureError(RuntimeError):
    pass


class OfficialDisclosureProvider:
    """Discover CNINFO records and archive only explicitly allowlisted official URLs."""

    def __init__(
        self,
        *,
        session: Optional[requests.Session] = None,
        documents_root: Optional[Path] = None,
        allowed_hosts: Optional[Iterable[str]] = None,
        timeout_seconds: int = 20,
        max_document_bytes: int = 50 * 1024 * 1024,
    ) -> None:
        self.session = session or requests.Session()
        self.documents_root = (
            documents_root or Path(os.getenv("RESEARCH_DOCUMENTS_DIR", "./data/research/documents"))
        ).expanduser().resolve()
        self.allowed_hosts = frozenset(
            host.strip().lower()
            for host in (
                allowed_hosts
                or (os.getenv("RESEARCH_DISCLOSURE_ALLOWED_HOSTS") or "").split(",")
                or DEFAULT_ALLOWED_DISCLOSURE_HOSTS
            )
            if host.strip()
        ) or DEFAULT_ALLOWED_DISCLOSURE_HOSTS
        self.timeout_seconds = max(1, min(int(timeout_seconds), 120))
        self.max_document_bytes = max(1024, int(max_document_bytes))
        self._stock_identity_cache: dict[str, str] = {}
        self.session.headers.update(
            {
                "User-Agent": "daily-stock-analysis/PEI-research (+local archival)",
                "Accept": "application/json,application/pdf,text/plain,*/*",
            }
        )

    def discover_cninfo(
        self,
        *,
        symbol: str,
        exchange: str,
        page: int = 1,
        page_size: int = 30,
        category: str = "",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        endpoint = os.getenv("RESEARCH_CNINFO_QUERY_URL", "https://www.cninfo.com.cn/new/hisAnnouncement/query")
        self._validate_url(endpoint)
        column, plate = _cninfo_market(exchange)
        org_id = self.resolve_cninfo_org_id(symbol)
        date_range = f"{start_date or ''}~{end_date or ''}" if start_date or end_date else ""
        response = self.session.post(
            endpoint,
            data={
                "pageNum": max(1, int(page)),
                "pageSize": max(1, min(int(page_size), 100)),
                "column": column,
                "tabName": "fulltext",
                "plate": plate,
                "stock": f"{symbol},{org_id}",
                "searchkey": "",
                "secid": "",
                "category": category,
                "trade": "",
                "seDate": date_range,
                "sortName": "",
                "sortType": "",
                "isHLtitle": "true",
            },
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        self._validate_response_chain(response)
        response.raise_for_status()
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OfficialDisclosureError("CNINFO returned invalid JSON") from exc
        announcements = payload.get("announcements") if isinstance(payload, Mapping) else None
        if not isinstance(announcements, list):
            return []
        results = []
        for item in announcements:
            if not isinstance(item, Mapping):
                continue
            adjunct = str(item.get("adjunctUrl") or "").strip()
            if not adjunct:
                continue
            document_url = urljoin("https://static.cninfo.com.cn/", adjunct)
            self._validate_url(document_url)
            announcement_time = item.get("announcementTime")
            if isinstance(announcement_time, (int, float)):
                published_at = datetime.fromtimestamp(announcement_time / 1000, tz=timezone.utc)
            else:
                published_at = _parse_datetime(item.get("announcementTime") or item.get("announcementDate"))
            external_id = str(item.get("announcementId") or hashlib.sha256(document_url.encode()).hexdigest())
            results.append(
                {
                    "source_name": "cninfo",
                    "external_id": external_id,
                    "title": _strip_html(str(item.get("announcementTitle") or "公告")),
                    "document_type": _document_type(str(item.get("announcementTitle") or "")),
                    "published_at": published_at,
                    "available_at": published_at,
                    "period_end": _period_end(str(item.get("announcementTitle") or "")),
                    "url": document_url,
                    "metadata": {"org_id": item.get("orgId"), "sec_code": item.get("secCode")},
                }
            )
        return results

    def resolve_cninfo_org_id(self, symbol: str) -> str:
        normalized = symbol.strip()
        if not normalized.isdigit() or len(normalized) != 6:
            raise ValueError("CNINFO symbol must be a six-digit A-share code")
        cached = self._stock_identity_cache.get(normalized)
        if cached:
            return cached
        endpoint = os.getenv(
            "RESEARCH_CNINFO_STOCK_LIST_URL",
            "https://www.cninfo.com.cn/new/data/szse_stock.json",
        )
        self._validate_url(endpoint)
        response = self.session.get(
            endpoint,
            timeout=self.timeout_seconds,
            allow_redirects=True,
        )
        self._validate_response_chain(response)
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > 5 * 1024 * 1024:
            raise OfficialDisclosureError("CNINFO stock list exceeds configured safety limit")
        content = response.content
        if len(content) > 5 * 1024 * 1024:
            raise OfficialDisclosureError("CNINFO stock list exceeds configured safety limit")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise OfficialDisclosureError("CNINFO stock list returned invalid JSON") from exc
        rows = payload.get("stockList") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise OfficialDisclosureError("CNINFO stock list is missing stockList")
        for item in rows:
            if not isinstance(item, Mapping) or str(item.get("code") or "") != normalized:
                continue
            org_id = str(item.get("orgId") or "").strip()
            if not org_id or len(org_id) > 64:
                break
            self._stock_identity_cache[normalized] = org_id
            return org_id
        raise OfficialDisclosureError(f"CNINFO organization id not found for {normalized}")

    def archive(self, *, security_id: str, external_id: str, url: str) -> dict[str, Any]:
        self._validate_url(url)
        response = self.session.get(
            url,
            timeout=self.timeout_seconds,
            allow_redirects=True,
            stream=True,
        )
        self._validate_response_chain(response)
        response.raise_for_status()
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > self.max_document_bytes:
            raise OfficialDisclosureError("official disclosure exceeds configured size limit")
        chunks = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > self.max_document_bytes:
                raise OfficialDisclosureError("official disclosure exceeds configured size limit")
            chunks.append(chunk)
        content = b"".join(chunks)
        sha256 = hashlib.sha256(content).hexdigest()
        suffix = ".pdf" if content.startswith(b"%PDF") else ".bin"
        directory = (self.documents_root / security_id).resolve()
        if not directory.is_relative_to(self.documents_root):
            raise OfficialDisclosureError("document directory escaped configured root")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        safe_id = hashlib.sha256(external_id.encode("utf-8")).hexdigest()[:32]
        document_path = directory / f"{safe_id}-{sha256[:16]}{suffix}"
        if not document_path.exists():
            _atomic_write_bytes(document_path, content)
        text_path = None
        parse_warning = None
        if suffix == ".pdf":
            try:
                extracted = _extract_pdf_text(content)
            except OfficialDisclosureError as exc:
                extracted = None
                parse_warning = str(exc)
            if extracted:
                text_path = directory / f"{safe_id}-{sha256[:16]}.txt"
                if not text_path.exists():
                    _atomic_write_bytes(text_path, extracted.encode("utf-8"))
        return {
            "storage_path": str(document_path),
            "parsed_text_path": str(text_path) if text_path else None,
            "sha256": f"sha256:{sha256}",
            "size_bytes": total,
            "content_type": response.headers.get("Content-Type"),
            "final_url": response.url,
            "parse_warning": parse_warning,
        }

    def _validate_response_chain(self, response: requests.Response) -> None:
        for item in [*response.history, response]:
            self._validate_url(item.url)

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            raise OfficialDisclosureError("official disclosure URLs must use HTTPS")
        hostname = (parsed.hostname or "").lower().rstrip(".")
        if hostname not in self.allowed_hosts:
            raise OfficialDisclosureError(f"official disclosure host is not allowlisted: {hostname or '?'}")
        if parsed.username or parsed.password or parsed.port not in (None, 443):
            raise OfficialDisclosureError("official disclosure URL contains forbidden authority fields")


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".disclosure-", dir=str(path.parent))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if os.path.exists(temporary):
            os.unlink(temporary)


def _extract_pdf_text(content: bytes) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        parts = []
        for page in reader.pages[:500]:
            text = page.extract_text() or ""
            if text:
                parts.append(text)
            if sum(len(part) for part in parts) >= 2_000_000:
                break
        return "\n\n".join(parts)[:2_000_000]
    except Exception as exc:
        raise OfficialDisclosureError(f"official PDF text extraction failed: {type(exc).__name__}") from exc


def _cninfo_market(exchange: str) -> tuple[str, str]:
    normalized = exchange.upper()
    if normalized in {"SSE", "SH"}:
        return "sse", "sh"
    if normalized in {"SZSE", "SZ"}:
        return "szse", "sz"
    if normalized in {"BSE", "BJ"}:
        return "third", "bj"
    raise ValueError(f"unsupported A-share exchange: {exchange}")


def _strip_html(value: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", value).strip()


def _document_type(title: str) -> str:
    if "年度报告" in title or "年报" in title:
        return "annual_report"
    if "半年度报告" in title or "半年报" in title:
        return "semiannual_report"
    if "季度报告" in title or "季报" in title:
        return "quarterly_report"
    if "业绩快报" in title:
        return "earnings_release"
    if "业绩预告" in title:
        return "earnings_forecast"
    return "announcement"


def _period_end(title: str) -> Optional[str]:
    import re

    match = re.search(r"(20\d{2})年(?:第([一二三四])季度|半年度|年度)", title)
    if not match:
        return None
    year = match.group(1)
    quarter = match.group(2)
    if "半年度" in match.group(0):
        return f"{year}-06-30"
    if "年度" in match.group(0) and "半年度" not in match.group(0):
        return f"{year}-12-31"
    return {
        "一": f"{year}-03-31",
        "二": f"{year}-06-30",
        "三": f"{year}-09-30",
        "四": f"{year}-12-31",
    }.get(quarter)


def _parse_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise OfficialDisclosureError("official disclosure is missing publish time")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        from zoneinfo import ZoneInfo

        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return parsed.astimezone(timezone.utc)
