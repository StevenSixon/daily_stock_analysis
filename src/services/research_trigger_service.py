# -*- coding: utf-8 -*-
"""Idempotent mapping from DSA events to PEI research workflows."""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional


logger = logging.getLogger(__name__)


PERIODIC_DOCUMENT_TYPES = frozenset(
    {"annual_report", "semiannual_report", "quarterly_report", "earnings_release"}
)
MAJOR_ANNOUNCEMENT_TERMS = (
    "重大资产重组",
    "收购",
    "出售资产",
    "控制权",
    "回购",
    "增发",
    "诉讼",
    "仲裁",
    "立案",
    "处罚",
    "停牌",
    "业绩预告",
    "业绩快报",
)


def _enabled(name: str) -> bool:
    return (os.getenv(name) or "false").strip().lower() in {"1", "true", "yes", "on"}


class ResearchTriggerService:
    def __init__(self, research_service: Any) -> None:
        self.research_service = research_service

    def on_disclosure(
        self,
        *,
        security_code: str,
        document: Mapping[str, Any],
        as_of: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if not _enabled("RESEARCH_AUTO_TRIGGER_DISCLOSURES"):
            return {"status": "disabled"}
        document_type = str(document.get("document_type") or "announcement").lower()
        title = str(document.get("title") or "")
        if document_type in PERIODIC_DOCUMENT_TYPES:
            workflow = "earnings_deep_dive"
            priority = 30
        elif any(term in title for term in MAJOR_ANNOUNCEMENT_TERMS):
            workflow = "thesis_update"
            priority = 50
        else:
            return {"status": "ignored", "reason": "non_material_disclosure"}
        event_id = str(document.get("external_id") or document.get("document_id") or "").strip()
        if not event_id:
            return {"status": "ignored", "reason": "missing_source_event_id"}
        job, created = self.research_service.create_job(
            security_code=security_code,
            workflow=workflow,
            as_of=as_of or datetime.now(timezone.utc),
            trigger_reason="official_disclosure",
            source_event_id=f"disclosure:{event_id}",
            priority=priority,
        )
        return {"status": "created" if created else "deduplicated", "job": job}

    def on_alert(
        self,
        *,
        security_code: str,
        trigger_id: int,
        result: Optional[Mapping[str, Any]] = None,
        as_of: Optional[datetime] = None,
    ) -> dict[str, Any]:
        if not _enabled("RESEARCH_AUTO_TRIGGER_ALERTS"):
            return {"status": "disabled"}
        job, created = self.research_service.create_job(
            security_code=security_code,
            workflow="long_short_pitch",
            as_of=as_of or datetime.now(timezone.utc),
            trigger_reason="dsa_technical_signal",
            source_event_id=f"alert:{int(trigger_id)}",
            priority=200,
        )
        return {
            "status": "created" if created else "deduplicated",
            "job": job,
            "signal": {
                key: result.get(key)
                for key in ("observed_value", "threshold", "data_timestamp")
                if result and result.get(key) is not None
            },
        }


class ResearchDisclosureScanner:
    """Periodic, failure-contained scanner used only by schedule mode when opted in."""

    def __init__(self, research_service: Any) -> None:
        self.research_service = research_service

    def run_once(
        self,
        security_codes: Iterable[str],
        *,
        lookback_days: int = 45,
    ) -> dict[str, int]:
        stats = {
            "securities": 0,
            "refreshed": 0,
            "failed": 0,
            "discovered": 0,
            "created": 0,
            "research_jobs": 0,
        }
        seen: set[str] = set()
        for raw_code in security_codes:
            code = str(raw_code or "").strip().upper()
            if not code or code in seen:
                continue
            seen.add(code)
            stats["securities"] += 1
            try:
                result = self.research_service.refresh_disclosures(
                    code,
                    lookback_days=lookback_days,
                )
                disclosures = result.get("disclosures") or {}
                triggers = result.get("research_triggers") or []
                stats["refreshed"] += 1
                stats["discovered"] += int(disclosures.get("discovered") or 0)
                stats["created"] += int(disclosures.get("created") or 0)
                stats["research_jobs"] += sum(
                    1 for item in triggers if isinstance(item, Mapping) and item.get("status") == "created"
                )
            except Exception as exc:
                stats["failed"] += 1
                logger.warning(
                    "[ResearchDisclosureScanner] Failed to refresh %s: %s",
                    code,
                    type(exc).__name__,
                )
        return stats
