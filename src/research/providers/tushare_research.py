# -*- coding: utf-8 -*-
"""Tushare Pro adapter for research-grade structured A-share data."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, time, timezone
from typing import Any, Mapping, Optional

from src.research.normalizer import normalize_tushare_fact, tushare_available_at


STATEMENT_METRICS: dict[str, dict[str, str]] = {
    "income": {
        "revenue": "revenue",
        "operate_profit": "operate_profit",
        "total_profit": "total_profit",
        "n_income": "net_income",
        "n_income_attr_p": "net_income_parent",
        "basic_eps": "basic_eps",
    },
    "balance": {
        "total_assets": "total_assets",
        "total_liab": "total_liabilities",
        "total_hldr_eqy_exc_min_int": "equity_parent",
        "money_cap": "cash_and_equivalents",
        "total_cur_assets": "current_assets",
        "total_cur_liab": "current_liabilities",
    },
    "cashflow": {
        "n_cashflow_act": "operating_cash_flow",
        "n_cashflow_inv_act": "investing_cash_flow",
        "n_cash_flows_fnc_act": "financing_cash_flow",
        "c_pay_acq_const_fiolta": "capital_expenditure",
        "n_incr_cash_cash_equ": "net_change_cash",
    },
    "indicator": {
        "roe": "roe",
        "roa": "roa",
        "grossprofit_margin": "gross_margin",
        "netprofit_margin": "net_margin",
        "debt_to_assets": "debt_to_assets",
        "current_ratio": "current_ratio",
        "quick_ratio": "quick_ratio",
        "ocfps": "operating_cash_flow_per_share",
    },
}


class TushareResearchProvider:
    """Fetch structured records without mixing them into the daily-price fetcher."""

    def __init__(self, pro_client: Any) -> None:
        self.pro = pro_client
        self.warnings: list[str] = []

    @classmethod
    def from_env(cls) -> "TushareResearchProvider":
        token = (os.getenv("TUSHARE_TOKEN") or "").strip()
        if not token:
            raise ValueError("TUSHARE_TOKEN is required for online research ingestion")
        import tushare as ts

        return cls(ts.pro_api(token))

    def capabilities(self) -> dict[str, bool]:
        return {
            "security_master": hasattr(self.pro, "stock_basic"),
            "income_statement": hasattr(self.pro, "income"),
            "balance_sheet": hasattr(self.pro, "balancesheet"),
            "cash_flow": hasattr(self.pro, "cashflow"),
            "financial_indicators": hasattr(self.pro, "fina_indicator"),
            "forecast": hasattr(self.pro, "forecast"),
            "express": hasattr(self.pro, "express"),
            "dividend": hasattr(self.pro, "dividend"),
            "daily_price": hasattr(self.pro, "daily"),
            "adjustment_factor": hasattr(self.pro, "adj_factor"),
        }

    def fetch_security(self, ts_code: str) -> dict[str, Any]:
        records = _records(
            self.pro.stock_basic(
                ts_code=ts_code,
                fields="ts_code,symbol,name,area,industry,market,exchange,list_status,list_date,delist_date",
            )
        )
        if not records:
            raise LookupError(f"Tushare security not found: {ts_code}")
        item = records[0]
        return {
            "ts_code": item["ts_code"],
            "symbol": item["symbol"],
            "exchange": _exchange(item.get("exchange"), item["ts_code"]),
            "market": "cn",
            "name": item.get("name") or item["symbol"],
            "industry": item.get("industry"),
            "currency": "CNY",
            "list_status": _list_status(item.get("list_status")),
            "listed_at": item.get("list_date"),
            "delisted_at": item.get("delist_date"),
            "profile": {
                "area": item.get("area"),
                "board": item.get("market"),
                "source": "tushare",
            },
        }

    def fetch_financial_facts(
        self,
        *,
        security_id: str,
        ts_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        endpoint_specs = (
            ("income", "income", STATEMENT_METRICS["income"]),
            ("balancesheet", "balance", STATEMENT_METRICS["balance"]),
            ("cashflow", "cashflow", STATEMENT_METRICS["cashflow"]),
            ("fina_indicator", "indicator", STATEMENT_METRICS["indicator"]),
        )
        facts: list[dict[str, Any]] = []
        self.warnings = []
        for endpoint_name, statement_type, metrics in endpoint_specs:
            endpoint = getattr(self.pro, endpoint_name)
            try:
                records = _records(
                    endpoint(
                        ts_code=ts_code,
                        start_date=start_date,
                        end_date=end_date,
                    )
                )
            except Exception as exc:
                self.warnings.append(f"{endpoint_name}_unavailable:{type(exc).__name__}")
                continue
            for row_index, record in enumerate(records):
                record_base = _record_identity(endpoint_name, record, row_index)
                for source_field, metric_code in metrics.items():
                    if statement_type == "indicator":
                        unit = "RATIO"
                    elif source_field.endswith("eps"):
                        unit = "CNY_PER_SHARE"
                    else:
                        unit = "CNY"
                    fact = normalize_tushare_fact(
                        record,
                        security_id=security_id,
                        statement_type=statement_type,
                        metric_code=metric_code,
                        value_field=source_field,
                        source_record_id=f"{record_base}:{source_field}",
                        unit=unit,
                    )
                    if fact is not None:
                        facts.append(fact)
        return facts

    def fetch_corporate_actions(self, *, security_id: str, ts_code: str) -> list[dict[str, Any]]:
        if not hasattr(self.pro, "dividend"):
            return []
        results = []
        for index, record in enumerate(_records(self.pro.dividend(ts_code=ts_code))):
            announced_at = tushare_available_at(record)
            identity = _record_identity("dividend", record, index)
            results.append(
                {
                    "security_id": security_id,
                    "action_type": "dividend",
                    "announced_at": announced_at,
                    "available_at": announced_at,
                    "record_date": record.get("record_date"),
                    "ex_date": record.get("ex_date"),
                    "effective_date": record.get("pay_date"),
                    "amount_per_share": record.get("cash_div_tax"),
                    "ratio": record.get("stk_div"),
                    "currency": "CNY",
                    "source_name": "tushare",
                    "source_record_id": identity,
                    "metadata": record,
                }
            )
        return results

    def fetch_market_prices(
        self,
        *,
        security_id: str,
        ts_code: str,
        start_date: str,
        end_date: str,
        basis: str = "raw",
    ) -> list[dict[str, Any]]:
        if basis not in {"raw", "forward", "backward"}:
            raise ValueError("unsupported market price basis")
        daily = _records(self.pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date))
        factors = {
            str(item.get("trade_date")): item.get("adj_factor")
            for item in _records(self.pro.adj_factor(ts_code=ts_code, start_date=start_date, end_date=end_date))
        }
        if basis != "raw" and daily and not any(factors.get(str(item.get("trade_date"))) for item in daily):
            raise ValueError("adjusted price basis requested but adjustment factors are unavailable")
        factor_dates = sorted(key for key, value in factors.items() if value not in (None, ""))
        latest_factor = float(factors[factor_dates[-1]]) if factor_dates else None
        oldest_factor = float(factors[factor_dates[0]]) if factor_dates else None
        results = []
        for item in daily:
            trade_date = str(item["trade_date"])
            factor = float(factors[trade_date]) if factors.get(trade_date) not in (None, "") else None
            multiplier = 1.0
            if basis == "forward":
                if factor is None or latest_factor in (None, 0):
                    raise ValueError(f"missing adjustment factor for {trade_date}")
                multiplier = factor / latest_factor
            elif basis == "backward":
                if factor is None or oldest_factor in (None, 0):
                    raise ValueError(f"missing adjustment factor for {trade_date}")
                multiplier = factor / oldest_factor
            available_at = _price_available_at(trade_date)
            results.append(
                {
                    "security_id": security_id,
                    "trade_date": trade_date,
                    "basis": basis,
                    "open": _scaled(item.get("open"), multiplier),
                    "high": _scaled(item.get("high"), multiplier),
                    "low": _scaled(item.get("low"), multiplier),
                    "close": _scaled(item.get("close"), multiplier),
                    "volume": item.get("vol"),
                    "amount": item.get("amount"),
                    "adj_factor": factor,
                    "currency": "CNY",
                    "source_name": "tushare",
                    "available_at": available_at,
                }
            )
        return results


def _records(frame: Any) -> list[dict[str, Any]]:
    if frame is None:
        return []
    if isinstance(frame, list):
        return [dict(item) for item in frame if isinstance(item, Mapping)]
    if hasattr(frame, "to_dict"):
        records = frame.to_dict("records")
        return [
            {key: _none_if_nan(value) for key, value in dict(item).items()}
            for item in records
        ]
    raise TypeError("Tushare response must be a DataFrame or list of mappings")


def _none_if_nan(value: Any) -> Any:
    try:
        if value != value:
            return None
    except Exception:
        pass
    return value


def _record_identity(endpoint: str, record: Mapping[str, Any], index: int) -> str:
    raw = "|".join(
        str(record.get(key) or "")
        for key in ("ts_code", "end_date", "ann_date", "f_ann_date", "report_type", "update_flag")
    )
    digest = hashlib.sha256(f"{endpoint}|{raw}|{index}".encode("utf-8")).hexdigest()[:32]
    return f"{endpoint}:{digest}"


def _exchange(value: Any, ts_code: str) -> str:
    normalized = str(value or "").upper()
    if normalized in {"SSE", "SZSE", "BSE"}:
        return normalized
    suffix = ts_code.rsplit(".", 1)[-1].upper()
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, normalized or "UNKNOWN")


def _list_status(value: Any) -> str:
    return {"L": "listed", "D": "delisted", "P": "paused"}.get(str(value or "").upper(), "listed")


def _price_available_at(trade_date: str) -> datetime:
    from zoneinfo import ZoneInfo

    local = datetime.combine(datetime.strptime(trade_date, "%Y%m%d").date(), time(16, 0))
    return local.replace(tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc)


def _scaled(value: Any, multiplier: float) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value) * multiplier
