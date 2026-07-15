# -*- coding: utf-8 -*-
"""Deterministic workflow-specific quality gates for Evidence Packs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class QualityGateResult:
    status: str
    coverage: dict[str, Any]
    warnings: tuple[str, ...]
    blocking_gaps: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "coverage": self.coverage,
            "warnings": list(self.warnings),
            "blocking_gaps": list(self.blocking_gaps),
        }


class ResearchQualityGate:
    """Refuse workflows whose required point-in-time evidence is absent."""

    def evaluate(
        self,
        *,
        workflow: str,
        financial_facts: Sequence[Mapping[str, Any]],
        filings: Sequence[Mapping[str, Any]],
        prices: Sequence[Mapping[str, Any]],
        corporate_actions: Sequence[Mapping[str, Any]],
    ) -> QualityGateResult:
        normalized = workflow.strip().lower()
        periods = {str(item.get("period_end") or "") for item in financial_facts if item.get("period_end")}
        statements = {
            str(item.get("statement_type") or "").lower()
            for item in financial_facts
            if item.get("statement_type")
        }
        periodic_filings = [
            item
            for item in filings
            if str(item.get("document_type") or "").lower()
            in {"annual_report", "semiannual_report", "quarterly_report", "earnings_release"}
        ]
        coverage = {
            "financial_fact_count": len(financial_facts),
            "financial_period_count": len(periods),
            "statement_types": sorted(statements),
            "filing_count": len(filings),
            "periodic_filing_count": len(periodic_filings),
            "market_price_count": len(prices),
            "corporate_action_count": len(corporate_actions),
        }
        warnings: list[str] = []
        blocking: list[str] = []

        if not financial_facts:
            warnings.append("financial_facts_missing")
        if not filings:
            warnings.append("official_filings_missing")
        if not prices:
            warnings.append("market_prices_missing")

        if normalized == "earnings_deep_dive":
            if not periodic_filings:
                blocking.append("periodic_filing_required")
            if len(periods) < 2:
                blocking.append("two_comparable_financial_periods_required")
        elif normalized in {"initiating_coverage", "dcf", "comps_valuation"}:
            missing_statements = sorted({"income", "balance", "cashflow"} - statements)
            if missing_statements:
                blocking.append(f"complete_statements_required:{','.join(missing_statements)}")
            if len(periods) < 3:
                blocking.append("three_historical_periods_required")
            if not prices:
                blocking.append("declared_market_price_basis_required")
        elif normalized in {"thesis_update", "thesis_tracker"}:
            if not filings:
                blocking.append("trigger_filing_required")
        elif normalized in {"long_short_pitch", "catalyst_calendar", "earnings_preview"}:
            if not prices and not filings:
                blocking.append("market_or_filing_evidence_required")
        else:
            blocking.append(f"unsupported_workflow:{normalized}")

        if blocking:
            status = "blocked_data"
        elif warnings:
            status = "degraded"
        else:
            status = "ready"
        return QualityGateResult(
            status=status,
            coverage=coverage,
            warnings=tuple(dict.fromkeys(warnings)),
            blocking_gaps=tuple(dict.fromkeys(blocking)),
        )
