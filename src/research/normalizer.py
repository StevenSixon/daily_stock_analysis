# -*- coding: utf-8 -*-
"""Deterministic normalization helpers for A-share research facts."""

from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any, Mapping, Optional


UNIT_MULTIPLIERS = {
    "CNY": Decimal("1"),
    "CNY_PER_SHARE": Decimal("1"),
    "CNY_THOUSAND": Decimal("1000"),
    "CNY_TEN_THOUSAND": Decimal("10000"),
    "CNY_MILLION": Decimal("1000000"),
}


def normalize_amount(value: Any, *, unit: str) -> float:
    """Convert a declared CNY unit into base CNY without guessing the source unit."""
    normalized_unit = unit.strip().upper()
    if normalized_unit not in UNIT_MULTIPLIERS:
        raise ValueError(f"unsupported financial unit: {unit}")
    return float(Decimal(str(value)) * UNIT_MULTIPLIERS[normalized_unit])


def cumulative_to_single_quarter(current: Any, previous: Any) -> float:
    """Derive one quarter from two cumulative values using decimal arithmetic."""
    return float(Decimal(str(current)) - Decimal(str(previous)))


def tushare_available_at(record: Mapping[str, Any]) -> datetime:
    """Map Tushare disclosure dates to a conservative UTC availability boundary."""
    raw = str(record.get("f_ann_date") or record.get("ann_date") or "").strip()
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError("Tushare record requires f_ann_date or ann_date in YYYYMMDD format")
    local = datetime.combine(datetime.strptime(raw, "%Y%m%d").date(), time(23, 59, 59))
    # Mainland disclosure dates do not contain a precise timestamp. Treat the
    # data as available only after the whole Asia/Shanghai disclosure day.
    from zoneinfo import ZoneInfo

    return local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)


def normalize_tushare_fact(
    record: Mapping[str, Any],
    *,
    security_id: str,
    statement_type: str,
    metric_code: str,
    value_field: str,
    source_record_id: str,
    unit: str = "CNY",
    document_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Create an append-only FinancialFact payload from one Tushare record."""
    value = record.get(value_field)
    if value in (None, ""):
        return None
    period_end = str(record.get("end_date") or "").strip()
    if len(period_end) != 8 or not period_end.isdigit():
        raise ValueError("Tushare financial record requires end_date in YYYYMMDD format")
    available_at = tushare_available_at(record)
    update_flag = str(record.get("update_flag") or "0").strip()
    revision_no = 1 if update_flag == "1" else 0
    normalized_value = normalize_amount(value, unit=unit) if unit != "RATIO" else float(value)
    normalized_unit = {
        "RATIO": "ratio",
        "CNY_PER_SHARE": "CNY/share",
    }.get(unit, "CNY")
    return {
        "security_id": security_id,
        "metric_code": metric_code,
        "statement_type": statement_type,
        "period_end": period_end,
        "announced_at": available_at,
        "available_at": available_at,
        "value": normalized_value,
        "unit": normalized_unit,
        "currency": "CNY",
        "scope": "consolidated",
        "report_type": str(record.get("report_type") or "periodic"),
        "source_name": "tushare",
        "source_record_id": source_record_id,
        "document_id": document_id,
        "revision_no": revision_no,
        "transform_version": "tushare-normalizer-v1",
        "quality": "structured_source",
        "raw": dict(record),
    }
