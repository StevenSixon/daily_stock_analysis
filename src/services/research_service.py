# -*- coding: utf-8 -*-
"""Application façade for Research Center, Worker API, and event triggers."""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from src.integrations.codex.output_validator import PeiOutputValidationError, PeiOutputValidator
from src.research.database import ResearchDatabase
from src.research.evidence_pack import EvidencePackBuilder
from src.research.ingestion import ResearchIngestionService
from src.research.providers.official_disclosure import OfficialDisclosureProvider
from src.research.providers.tushare_research import TushareResearchProvider
from src.research.repositories import ResearchRepository, json_loads, model_dict


logger = logging.getLogger(__name__)


SUPPORTED_RESEARCH_WORKFLOWS = frozenset(
    {
        "earnings_deep_dive",
        "earnings_preview",
        "initiating_coverage",
        "thesis_update",
        "thesis_tracker",
        "catalyst_calendar",
        "dcf",
        "comps_valuation",
        "long_short_pitch",
    }
)


class ResearchServiceError(RuntimeError):
    error_code = "research_service_error"


class ResearchNotFoundError(ResearchServiceError):
    error_code = "research_not_found"


class ResearchConflictError(ResearchServiceError):
    error_code = "research_conflict"


class ResearchDataBlockedError(ResearchServiceError):
    error_code = "research_data_blocked"


class ResearchOutputValidationError(ResearchServiceError):
    error_code = "research_output_invalid"

    def __init__(self, message: str, issues: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.issues = issues


def research_enabled() -> bool:
    return (os.getenv("RESEARCH_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"}


class ResearchService:
    def __init__(
        self,
        *,
        database: Optional[ResearchDatabase] = None,
        repository: Optional[ResearchRepository] = None,
        evidence_builder: Optional[EvidencePackBuilder] = None,
        disclosure_provider: Optional[OfficialDisclosureProvider] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        self.database = database or ResearchDatabase()
        self.repository = repository or ResearchRepository(self.database)
        self.evidence_builder = evidence_builder or EvidencePackBuilder(self.repository)
        self.disclosure_provider = disclosure_provider or OfficialDisclosureProvider()
        self.notifier = notifier

    def status(self) -> dict[str, Any]:
        monthly_usage = self.repository.token_usage_since(_month_start_utc_naive())
        return {
            "enabled": research_enabled(),
            "database_ready": True,
            "tushare_configured": bool((os.getenv("TUSHARE_TOKEN") or "").strip()),
            "worker_token_configured": len((os.getenv("RESEARCH_WORKER_TOKEN") or "").strip()) >= 32,
            "plugin_skill": (os.getenv("RESEARCH_PEI_PLUGIN_SKILL") or "").strip() or None,
            "plugin_version": (os.getenv("RESEARCH_PEI_PLUGIN_VERSION") or "").strip() or None,
            "supported_workflows": sorted(SUPPORTED_RESEARCH_WORKFLOWS),
            "run_token_budget": _nonnegative_int_env("RESEARCH_RUN_TOKEN_BUDGET", default=0),
            "monthly_token_budget": _nonnegative_int_env("RESEARCH_MONTHLY_TOKEN_BUDGET", default=0),
            "current_month_tokens": monthly_usage["total_tokens"],
        }

    def create_job(
        self,
        *,
        security_code: str,
        workflow: str,
        as_of: Optional[datetime] = None,
        trigger_reason: str = "manual",
        source_event_id: Optional[str] = None,
        priority: int = 100,
        idempotency_key: Optional[str] = None,
        price_basis: str = "raw",
    ) -> tuple[dict[str, Any], bool]:
        workflow = workflow.strip().lower()
        if workflow not in SUPPORTED_RESEARCH_WORKFLOWS:
            raise ValueError(f"unsupported research workflow: {workflow}")
        if price_basis not in {"raw", "forward", "backward"}:
            raise ValueError("unsupported price basis")
        security = self._ensure_security(security_code)
        as_of_value = _utc_naive(as_of or datetime.now(timezone.utc))
        workflow_version = (os.getenv("RESEARCH_PEI_WORKFLOW_VERSION") or f"{workflow}-v1").strip()
        key = idempotency_key or self._job_idempotency_key(
            security.ts_code,
            workflow,
            workflow_version,
            trigger_reason,
            source_event_id,
            as_of_value,
            price_basis,
        )
        row, created = self.repository.create_job(
            {
                "security_id": security.id,
                "workflow": workflow,
                "workflow_version": workflow_version,
                "trigger_reason": trigger_reason,
                "source_event_id": source_event_id,
                "priority": max(0, min(int(priority), 1000)),
                "idempotency_key": key,
                "max_retries": max(0, int(os.getenv("RESEARCH_JOB_MAX_RETRIES", "2"))),
                "metadata": {"requested_as_of": _iso(as_of_value), "price_basis": price_basis},
            }
        )
        if created:
            self.prepare_job(row.id, as_of=as_of_value, price_basis=price_basis)
            row = self.repository.get_job(row.id)
        return self.job_payload(row), created

    def prepare_job(self, job_id: str, *, as_of: datetime, price_basis: str = "raw") -> dict[str, Any]:
        job = self.repository.get_job(job_id)
        if job is None:
            raise ResearchNotFoundError(f"research job not found: {job_id}")
        security = self.repository.get_security(job.security_id)
        if security is None:
            raise ResearchNotFoundError(f"research security not found: {job.security_id}")
        self.repository.mark_collecting_data(job_id)
        try:
            pack = self.evidence_builder.build(
                security_identifier=security.id,
                workflow=job.workflow,
                as_of=as_of,
                price_basis=price_basis,
            )
            updated = self.repository.set_job_pack(
                job_id,
                pack["pack_id"],
                quality_status=pack["quality"]["status"],
            )
            return self.job_payload(updated)
        except Exception as exc:
            self.repository.block_job(
                job_id,
                error_code="evidence_pack_failed",
                error_message=f"Evidence Pack preparation failed: {type(exc).__name__}",
            )
            raise

    def refresh_security(
        self,
        security_code: str,
        *,
        years: int = 5,
        price_basis: str = "raw",
        include_disclosures: bool = True,
    ) -> dict[str, Any]:
        if not (os.getenv("TUSHARE_TOKEN") or "").strip():
            raise ResearchDataBlockedError("TUSHARE_TOKEN is required for online research refresh")
        provider = TushareResearchProvider.from_env()
        ingestion = ResearchIngestionService(
            self.repository,
            tushare_provider=provider,
        )
        ts_code = _normalize_ts_code(security_code)
        local_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start_date = local_today - timedelta(days=max(1, years) * 366)
        result = ingestion.ingest_tushare_security(
            ts_code=ts_code,
            start_date=start_date.strftime("%Y%m%d"),
            end_date=local_today.strftime("%Y%m%d"),
            price_basis=price_basis,
        )
        if include_disclosures:
            try:
                disclosure_result = self.refresh_disclosures(
                    ts_code,
                    lookback_days=max(1, years) * 366,
                )
                result["disclosures"] = disclosure_result["disclosures"]
                result["disclosure_research_triggers"] = disclosure_result["research_triggers"]
            except Exception as exc:
                result["warnings"].append(f"disclosure_refresh_failed:{type(exc).__name__}")
        return result

    def refresh_disclosures(
        self,
        security_code: str,
        *,
        lookback_days: int = 45,
        max_pages: Optional[int] = None,
    ) -> dict[str, Any]:
        """Discover new official disclosures and map only newly persisted rows to jobs."""
        security = self._ensure_security(security_code)
        ingestion = ResearchIngestionService(
            self.repository,
            disclosure_provider=self.disclosure_provider,
        )
        local_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        start_date = local_today - timedelta(days=max(1, min(int(lookback_days), 10 * 366)))
        page_limit = max_pages
        if page_limit is None:
            page_limit = _positive_int_env("RESEARCH_DISCLOSURE_MAX_PAGES", default=5, maximum=50)
        page_limit = max(1, min(int(page_limit), 50))
        combined = {
            "security_id": security.id,
            "discovered": 0,
            "created": 0,
            "archived": 0,
            "documents": [],
            "warnings": [],
        }
        for page in range(1, page_limit + 1):
            page_result = ingestion.ingest_cninfo_disclosures(
                security_identifier=security.id,
                page=page,
                page_size=100,
                start_date=start_date.strftime("%Y-%m-%d"),
                end_date=local_today.strftime("%Y-%m-%d"),
            )
            combined["discovered"] += int(page_result["discovered"])
            combined["created"] += int(page_result["created"])
            combined["archived"] += int(page_result["archived"])
            combined["documents"].extend(page_result["documents"])
            combined["warnings"].extend(page_result["warnings"])
            if int(page_result["discovered"]) < 100:
                break
        combined["warnings"] = list(dict.fromkeys(combined["warnings"]))

        from src.services.research_trigger_service import ResearchTriggerService

        trigger_service = ResearchTriggerService(self)
        triggers = []
        for document in combined["documents"]:
            if not document.get("created"):
                continue
            try:
                outcome = trigger_service.on_disclosure(
                    security_code=security.ts_code,
                    document=document,
                    as_of=_parse_datetime(document["published_at"]),
                )
            except Exception as exc:
                outcome = {"status": "failed", "error": type(exc).__name__}
            triggers.append(outcome)
        return {"disclosures": combined, "research_triggers": triggers}

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.repository.get_job(job_id)
        if row is None:
            raise ResearchNotFoundError(f"research job not found: {job_id}")
        return self.job_payload(row)

    def list_jobs(self, **kwargs) -> dict[str, Any]:
        rows, total = self.repository.list_jobs(**kwargs)
        return {"items": [self.job_payload(row) for row in rows], "total": total}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        try:
            return self.job_payload(self.repository.request_cancel(job_id))
        except KeyError as exc:
            raise ResearchNotFoundError(f"research job not found: {job_id}") from exc

    def claim_job(self, *, worker_id: str, lease_seconds: int) -> Optional[dict[str, Any]]:
        monthly_budget = _nonnegative_int_env("RESEARCH_MONTHLY_TOKEN_BUDGET", default=0)
        if monthly_budget:
            usage = self.repository.token_usage_since(_month_start_utc_naive())
            if usage["total_tokens"] >= monthly_budget:
                logger.warning(
                    "Research monthly token budget exhausted: %s/%s",
                    usage["total_tokens"],
                    monthly_budget,
                )
                return None
        claimed = self.repository.claim_job(
            worker_id=worker_id,
            lease_seconds=max(30, min(int(lease_seconds), 3600)),
        )
        if claimed is None:
            return None
        job, run = claimed
        if not job.pack_id:
            raise ResearchConflictError("claimable research job has no Evidence Pack")
        pack = self.evidence_builder.load(job.pack_id)
        return {
            "job": self.job_payload(job),
            "run_id": run.id,
            "lease_token": job.lease_token,
            "lease_expires_at": _iso(job.lease_expires_at),
            "pack": {
                "pack_id": pack["pack_id"],
                "manifest_hash": pack["manifest_hash"],
                "workflow": pack["workflow"],
                "as_of": pack["as_of"],
                "evidence_ids": [item["evidence_id"] for item in pack["evidence_manifest"]],
                "quality": pack["quality"],
            },
        }

    def heartbeat(self, job_id: str, *, lease_token: str, lease_seconds: int) -> dict[str, Any]:
        try:
            row = self.repository.heartbeat(
                job_id,
                lease_token=lease_token,
                lease_seconds=lease_seconds,
            )
        except KeyError as exc:
            raise ResearchNotFoundError(f"research job not found: {job_id}") from exc
        return {
            "job_id": row.id,
            "status": row.status,
            "cancel_requested": row.cancel_requested_at is not None or row.status == "cancel_requested",
            "lease_expires_at": _iso(row.lease_expires_at),
        }

    def complete_worker_run(
        self,
        job_id: str,
        *,
        run_id: str,
        lease_token: str,
        report: Mapping[str, Any],
        run_metadata: Mapping[str, Any],
        artifact_path: Optional[str],
    ) -> dict[str, Any]:
        job = self.repository.get_job(job_id)
        run = self.repository.get_run(run_id)
        if job is None or run is None or run.job_id != job_id:
            raise ResearchNotFoundError("research job or run not found")
        if not job.pack_id:
            raise ResearchConflictError("research job has no Evidence Pack")
        pack = self.evidence_builder.load(job.pack_id)
        try:
            self.repository.mark_validating(job_id, lease_token)
        except RuntimeError as exc:
            if "cancellation requested" not in str(exc):
                raise
            self._finish_cancelled_run(job_id, run_id, lease_token)
            raise ResearchConflictError("research job cancellation requested") from exc
        try:
            validated = PeiOutputValidator().validate(
                dict(report),
                expected_workflow=job.workflow,
                expected_as_of=pack["as_of"],
                allowed_evidence_ids={item["evidence_id"] for item in pack["evidence_manifest"]},
            )
        except PeiOutputValidationError as exc:
            issues = [item.to_dict() for item in exc.issues]
            self.repository.finish_run(
                run_id,
                {
                    "status": "failed",
                    "error_code": "schema_validation",
                    "error_message": "PEI output failed server-side validation",
                    "metadata": {"validation_issues": issues},
                },
            )
            self.repository.fail_job(
                job_id,
                lease_token,
                error_code="schema_validation",
                error_message="PEI output failed server-side validation",
                retryable=False,
            )
            raise ResearchOutputValidationError("PEI output failed server-side validation", issues) from exc

        usage = run_metadata.get("usage") if isinstance(run_metadata.get("usage"), Mapping) else {}
        normalized_usage = _normalized_usage(usage)
        budget_error = self._token_budget_error(normalized_usage)
        if budget_error:
            self.repository.finish_run(
                run_id,
                {
                    "status": "failed",
                    **normalized_usage,
                    "duration_seconds": run_metadata.get("duration_seconds"),
                    "exit_code": run_metadata.get("exit_code"),
                    "artifact_path": artifact_path,
                    "error_code": "token_budget_exceeded",
                    "error_message": budget_error,
                    "metadata": _safe_run_metadata(run_metadata),
                },
            )
            self.repository.fail_job(
                job_id,
                lease_token,
                error_code="token_budget_exceeded",
                error_message=budget_error,
                retryable=False,
            )
            raise ResearchConflictError(budget_error)
        self.repository.finish_run(
            run_id,
            {
                "status": "validated",
                "model": run_metadata.get("model"),
                "plugin_skill": run_metadata.get("plugin_skill"),
                "plugin_version": run_metadata.get("plugin_version"),
                "workflow_version": run_metadata.get("workflow_version"),
                "mcp_server_version": run_metadata.get("mcp_server_version"),
                **normalized_usage,
                "duration_seconds": run_metadata.get("duration_seconds"),
                "exit_code": run_metadata.get("exit_code"),
                "artifact_path": artifact_path,
                "metadata": _safe_run_metadata(run_metadata),
            },
        )
        manifest_types = {
            item["evidence_id"]: item["evidence_type"]
            for item in pack["evidence_manifest"]
        }
        references = [
            {"evidence_id": evidence_id, "evidence_type": manifest_types[evidence_id]}
            for evidence_id in sorted(_collect_evidence_ids(validated))
        ]
        try:
            report_row = self.repository.create_report_and_complete_job(
                {
                    "job_id": job.id,
                    "run_id": run_id,
                    "security_id": job.security_id,
                    "pack_id": job.pack_id,
                    "report_type": job.workflow,
                    "as_of": _parse_datetime(pack["as_of"]),
                    "structured": validated,
                    "markdown": validated["markdown"],
                    "artifact_path": artifact_path,
                    "model": run_metadata.get("model"),
                    "plugin_version": run_metadata.get("plugin_version"),
                    "workflow_version": job.workflow_version,
                },
                references,
                lease_token=lease_token,
            )
        except RuntimeError as exc:
            if "cancellation requested" not in str(exc):
                raise
            self._finish_cancelled_run(job_id, run_id, lease_token)
            raise ResearchConflictError("research job cancellation requested") from exc
        try:
            self.repository.replace_report_tracking_items(report_row, validated)
        except Exception as exc:
            logger.warning("Research report tracking materialization failed: %s", type(exc).__name__)
        return self.report_payload(report_row, include_content=True)

    def _finish_cancelled_run(self, job_id: str, run_id: str, lease_token: str) -> None:
        self.repository.finish_run(
            run_id,
            {
                "status": "cancelled",
                "error_code": "cancelled",
                "error_message": "Research job cancellation requested",
            },
        )
        self.repository.fail_job(
            job_id,
            lease_token,
            error_code="cancelled",
            error_message="Research job cancellation requested",
            retryable=False,
        )

    def fail_worker_run(
        self,
        job_id: str,
        *,
        run_id: str,
        lease_token: str,
        error_code: str,
        error_message: str,
        retryable: bool,
        run_metadata: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        run = self.repository.get_run(run_id)
        if run is None or run.job_id != job_id:
            raise ResearchNotFoundError("research run not found")
        metadata = dict(run_metadata or {})
        usage = metadata.get("usage") if isinstance(metadata.get("usage"), Mapping) else {}
        self.repository.finish_run(
            run_id,
            {
                "status": "failed",
                "error_code": error_code,
                "error_message": error_message,
                "duration_seconds": metadata.get("duration_seconds"),
                "exit_code": metadata.get("exit_code"),
                "artifact_path": metadata.get("artifact_path"),
                **_normalized_usage(usage),
                "metadata": _safe_run_metadata(metadata),
            },
        )
        row = self.repository.fail_job(
            job_id,
            lease_token,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
        )
        return self.job_payload(row)

    def _token_budget_error(self, usage: Mapping[str, Optional[int]]) -> Optional[str]:
        run_budget = _nonnegative_int_env("RESEARCH_RUN_TOKEN_BUDGET", default=0)
        monthly_budget = _nonnegative_int_env("RESEARCH_MONTHLY_TOKEN_BUDGET", default=0)
        if (run_budget or monthly_budget) and (
            usage.get("input_tokens") is None or usage.get("output_tokens") is None
        ):
            return "Research token usage is required while a token budget is enabled"
        run_total = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        if run_budget and run_total > run_budget:
            return f"Research run token budget exceeded: {run_total}/{run_budget}"
        if monthly_budget:
            current = self.repository.token_usage_since(_month_start_utc_naive())["total_tokens"]
            if current + run_total > monthly_budget:
                return f"Research monthly token budget exceeded: {current + run_total}/{monthly_budget}"
        return None

    def list_reports(self, **kwargs) -> dict[str, Any]:
        rows, total = self.repository.list_reports(**kwargs)
        return {"items": [self.report_payload(row) for row in rows], "total": total}

    def get_report(self, report_id: str) -> dict[str, Any]:
        row = self.repository.get_report(report_id)
        if row is None:
            raise ResearchNotFoundError(f"research report not found: {report_id}")
        return self.report_payload(row, include_content=True)

    def report_evidence(self, report_id: str) -> list[dict[str, Any]]:
        if self.repository.get_report(report_id) is None:
            raise ResearchNotFoundError(f"research report not found: {report_id}")
        return [model_dict(row) for row in self.repository.report_evidence(report_id)]

    def review_report(self, report_id: str, *, decision: str, note: Optional[str]) -> dict[str, Any]:
        try:
            row = self.repository.review_report(report_id, decision=decision, note=note)
        except KeyError as exc:
            raise ResearchNotFoundError(f"research report not found: {report_id}") from exc
        except ValueError as exc:
            raise ResearchConflictError(str(exc)) from exc
        payload = self.report_payload(row, include_content=True)
        if decision == "approve":
            self._notify_published_report(payload)
        return payload

    def _notify_published_report(self, report: Mapping[str, Any]) -> None:
        if (os.getenv("RESEARCH_NOTIFY_ON_PUBLISH") or "false").strip().lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return
        try:
            from src.notification import NotificationBuilder, NotificationService

            structured = report.get("structured") if isinstance(report.get("structured"), Mapping) else {}
            summary = str(structured.get("executive_summary") or "研究报告已通过人工审核并发布。")
            content = "\n\n".join(
                (
                    summary,
                    f"报告类型: {report.get('report_type')}",
                    f"as_of: {report.get('as_of')}",
                    f"报告 ID: {report.get('id')}",
                )
            )
            message = NotificationBuilder.build_simple_alert(
                title="PEI Research Report Published",
                content=content,
                alert_type="success",
            )
            notifier = self.notifier or NotificationService()
            notifier.send_with_results(
                message,
                route_type="report",
                dedup_key=f"research-report:{report.get('id')}",
                cooldown_key=f"research-report:{report.get('id')}",
            )
        except Exception as exc:
            logger.warning("Published research report notification failed: %s", type(exc).__name__)

    def security_timeline(self, security_code: str, *, limit: int = 100) -> dict[str, Any]:
        security = self.repository.get_security(security_code)
        if security is None:
            raise ResearchNotFoundError(f"research security not found: {security_code}")
        return {
            "security": self.security_payload(security),
            "items": [_json_safe(item) for item in self.repository.security_timeline(security.id, limit=limit)],
        }

    def get_document(self, document_id: str) -> dict[str, Any]:
        row = self.repository.get_document(document_id)
        if row is None:
            raise ResearchNotFoundError(f"research document not found: {document_id}")
        payload = model_dict(row)
        payload.pop("storage_path", None)
        payload.pop("parsed_text_path", None)
        payload["metadata"] = json_loads(payload.pop("metadata_json", None), {})
        return _json_safe(payload)

    def get_pack(self, pack_id: str) -> dict[str, Any]:
        try:
            return self.evidence_builder.load(pack_id)
        except KeyError as exc:
            raise ResearchNotFoundError(f"Evidence Pack not found: {pack_id}") from exc

    def pack_section(self, pack_id: str, section: str) -> Any:
        """Return one allow-listed frozen Evidence Pack section to the Worker."""
        pack = self.get_pack(pack_id)
        sections = {
            "manifest": lambda: {
                "pack_id": pack["pack_id"],
                "manifest_hash": pack["manifest_hash"],
                "schema_version": pack["schema_version"],
                "workflow": pack["workflow"],
                "as_of": pack["as_of"],
                "data_cutoff": pack["data_cutoff"],
                "security": pack["security"],
                "quality": pack["quality"],
                "evidence_manifest": pack["evidence_manifest"],
            },
            "company-profile": lambda: {
                "security": pack["security"],
                "company_profile": pack["company_profile"],
            },
            "financials": lambda: pack["financials"],
            "market-history": lambda: pack["market_data"],
            "corporate-actions": lambda: {"items": pack["corporate_actions"]},
            "filings": lambda: {"items": pack["filings"]},
            "previous-research": lambda: {"items": pack["previous_research"]},
        }
        resolver = sections.get(section)
        if resolver is None:
            raise ResearchNotFoundError(f"unsupported Evidence Pack section: {section}")
        return resolver()

    def pack_filing(self, pack_id: str, evidence_id: str) -> dict[str, Any]:
        pack = self.get_pack(pack_id)
        for item in pack["filings"]:
            if item.get("evidence_id") == evidence_id:
                return item
        raise ResearchNotFoundError(f"filing evidence not found in Evidence Pack: {evidence_id}")

    def resolve_pack_security(self, pack_id: str, code: str) -> dict[str, Any]:
        pack = self.get_pack(pack_id)
        normalized = code.strip().upper()
        security = pack["security"]
        accepted = {
            str(security.get("security_id") or "").upper(),
            str(security.get("ts_code") or "").upper(),
            str(security.get("symbol") or "").upper(),
        }
        if normalized not in accepted:
            raise ResearchNotFoundError(f"security {code!r} is outside Evidence Pack {pack_id}")
        return security

    def job_payload(self, row) -> dict[str, Any]:
        if row is None:
            return {}
        payload = model_dict(row)
        payload["metadata"] = json_loads(payload.pop("metadata_json", None), {})
        payload.pop("lease_token", None)
        return _json_safe(payload)

    def report_payload(self, row, *, include_content: bool = False) -> dict[str, Any]:
        payload = model_dict(row)
        structured = json_loads(payload.pop("structured_json", None), {})
        if include_content:
            payload["structured"] = structured
        else:
            payload["executive_summary"] = structured.get("executive_summary")
            payload.pop("markdown", None)
        return _json_safe(payload)

    @staticmethod
    def security_payload(row) -> dict[str, Any]:
        payload = model_dict(row)
        payload["profile"] = json_loads(payload.pop("profile_json", None), {})
        return _json_safe(payload)

    def _ensure_security(self, code: str):
        existing = self.repository.get_security(code)
        if existing is not None:
            return existing
        if not (os.getenv("TUSHARE_TOKEN") or "").strip():
            raise ResearchDataBlockedError(
                f"research security {code!r} is not loaded; configure TUSHARE_TOKEN and refresh it first"
            )
        provider = TushareResearchProvider.from_env()
        return self.repository.upsert_security(provider.fetch_security(_normalize_ts_code(code)))

    @staticmethod
    def _job_idempotency_key(
        ts_code: str,
        workflow: str,
        workflow_version: str,
        trigger_reason: str,
        source_event_id: Optional[str],
        as_of: datetime,
        price_basis: str,
    ) -> str:
        event_key = source_event_id or as_of.date().isoformat()
        raw = "|".join((ts_code, workflow, workflow_version, trigger_reason, event_key, price_basis))
        return f"research:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


@lru_cache(maxsize=1)
def get_research_service() -> ResearchService:
    if not research_enabled():
        raise ResearchServiceError("Research feature is disabled")
    return ResearchService()


def reset_research_service() -> None:
    get_research_service.cache_clear()


def _normalize_ts_code(code: str) -> str:
    normalized = code.strip().upper()
    if "." in normalized:
        return normalized
    if len(normalized) != 6 or not normalized.isdigit():
        raise ValueError("Phase 1 research currently supports six-digit A-share codes")
    if normalized.startswith(("4", "8", "9")):
        return f"{normalized}.BJ"
    if normalized.startswith(("5", "6", "9")):
        return f"{normalized}.SH"
    return f"{normalized}.SZ"


def _positive_int_env(name: str, *, default: int, maximum: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default
    return max(1, min(value, maximum))


def _nonnegative_int_env(name: str, *, default: int) -> int:
    try:
        value = int((os.getenv(name) or str(default)).strip())
    except ValueError:
        return default
    return max(0, min(value, 2_000_000_000))


def _normalized_usage(value: Mapping[str, Any]) -> dict[str, Optional[int]]:
    result: dict[str, Optional[int]] = {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
        raw = value.get(key)
        if isinstance(raw, bool) or raw is None:
            result[key] = None
            continue
        try:
            result[key] = max(0, int(raw))
        except (TypeError, ValueError):
            result[key] = None
    return result


def _month_start_utc_naive() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1)


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.isoformat().replace("+00:00", "Z")


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_datetime(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    return _utc_naive(datetime.fromisoformat(normalized))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return _iso(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _collect_evidence_ids(value: Any) -> set[str]:
    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "evidence_id" and isinstance(item, str):
                result.add(item)
            elif key == "evidence_ids" and isinstance(item, list):
                result.update(str(entry) for entry in item if isinstance(entry, str))
            else:
                result.update(_collect_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            result.update(_collect_evidence_ids(item))
    return result


def _safe_run_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema_version",
        "status",
        "workflow",
        "workflow_version",
        "output_schema_version",
        "output_schema_sha256",
        "model_output_schema_sha256",
        "as_of",
        "plugin_skill",
        "plugin_version",
        "model",
        "mcp_server_name",
        "mcp_server_version",
        "sandbox",
        "ephemeral",
        "started_at",
        "finished_at",
        "duration_seconds",
        "exit_code",
        "timed_out",
        "thread_id",
        "usage",
        "event_warnings",
        "mcp_tool_calls",
        "tool_item_counts",
        "transport_fallback",
    }
    return {key: _json_safe(value[key]) for key in allowed if key in value}
