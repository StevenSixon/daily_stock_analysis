# -*- coding: utf-8 -*-
"""Repository boundary for the independent PEI research domain."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import and_, desc, func, or_, select, update

from src.research.database import ResearchDatabase
from src.research.models import (
    Catalyst,
    CorporateAction,
    EvidencePackRecord,
    FinancialFact,
    MarketPriceBasis,
    ReportEvidence,
    ResearchJob,
    ResearchReport,
    ResearchRun,
    SecurityMaster,
    SourceDocument,
    ThesisItem,
    utc_now_naive,
)


CLAIMABLE_JOB_STATUSES = frozenset({"data_ready", "failed_retryable"})
TERMINAL_JOB_STATUSES = frozenset({"published", "cancelled", "failed_permanent"})
NON_CANCELLABLE_JOB_STATUSES = TERMINAL_JOB_STATUSES | frozenset(
    {"awaiting_review", "changes_requested", "rejected"}
)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def stable_id(prefix: str, value: str, *, length: int = 32) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}"


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def model_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class ResearchRepository:
    """Concrete SQLAlchemy repository; all writes flow through the DSA process."""

    def __init__(self, database: Optional[ResearchDatabase] = None) -> None:
        self.database = database or ResearchDatabase()

    # Security and source data -------------------------------------------------
    def upsert_security(self, payload: Mapping[str, Any]) -> SecurityMaster:
        ts_code = str(payload["ts_code"]).strip().upper()
        symbol = str(payload.get("symbol") or ts_code.split(".", 1)[0]).strip().upper()
        exchange = str(payload.get("exchange") or _exchange_from_ts_code(ts_code)).strip().upper()
        with self.database.session() as session:
            row = session.scalar(select(SecurityMaster).where(SecurityMaster.ts_code == ts_code))
            values = {
                "symbol": symbol,
                "exchange": exchange,
                "market": str(payload.get("market") or "cn").strip().lower(),
                "name": str(payload.get("name") or symbol).strip(),
                "industry": _optional_text(payload.get("industry")),
                "currency": str(payload.get("currency") or "CNY").strip().upper(),
                "list_status": str(payload.get("list_status") or "listed").strip().lower(),
                "listed_at": _optional_date(payload.get("listed_at")),
                "delisted_at": _optional_date(payload.get("delisted_at")),
                "profile_json": json_dumps(payload.get("profile") or {}),
            }
            if row is None:
                row = SecurityMaster(id=stable_id("sec", ts_code), ts_code=ts_code, **values)
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
                row.updated_at = utc_now_naive()
            session.flush()
            return row

    def get_security(self, identifier: str) -> Optional[SecurityMaster]:
        normalized = identifier.strip().upper()
        bare = normalized.split(".", 1)[0]
        with self.database.session() as session:
            return session.scalar(
                select(SecurityMaster).where(
                    or_(
                        SecurityMaster.id == identifier,
                        SecurityMaster.ts_code == normalized,
                        and_(SecurityMaster.symbol == bare, SecurityMaster.market == "cn"),
                    )
                )
            )

    def add_document(self, payload: Mapping[str, Any]) -> tuple[SourceDocument, bool]:
        source_name = str(payload["source_name"]).strip().lower()
        external_id = str(payload["external_id"]).strip()
        with self.database.session() as session:
            existing = session.scalar(
                select(SourceDocument).where(
                    SourceDocument.source_name == source_name,
                    SourceDocument.external_id == external_id,
                )
            )
            if existing is not None:
                return existing, False
            row = SourceDocument(
                id=stable_id("doc", f"{source_name}:{external_id}"),
                security_id=str(payload["security_id"]),
                source_name=source_name,
                external_id=external_id,
                document_type=str(payload.get("document_type") or "announcement"),
                title=str(payload["title"]).strip(),
                published_at=_required_datetime(payload["published_at"]),
                available_at=_required_datetime(payload.get("available_at") or payload["published_at"]),
                period_end=_optional_date(payload.get("period_end")),
                url=str(payload["url"]).strip(),
                storage_path=_optional_text(payload.get("storage_path")),
                parsed_text_path=_optional_text(payload.get("parsed_text_path")),
                sha256=_optional_text(payload.get("sha256")),
                size_bytes=_optional_int(payload.get("size_bytes")),
                revision_of_id=_optional_text(payload.get("revision_of_id")),
                metadata_json=json_dumps(payload.get("metadata") or {}),
                ingested_at=_required_datetime(payload.get("ingested_at") or utc_now_naive()),
            )
            session.add(row)
            session.flush()
            return row, True

    def get_document_by_source(self, source_name: str, external_id: str) -> Optional[SourceDocument]:
        with self.database.session() as session:
            return session.scalar(
                select(SourceDocument).where(
                    SourceDocument.source_name == source_name.strip().lower(),
                    SourceDocument.external_id == external_id.strip(),
                )
            )

    def update_document_archive(
        self,
        document_id: str,
        archive: Mapping[str, Any],
    ) -> SourceDocument:
        """Attach a successfully downloaded artifact to a previously discovered row."""
        with self.database.session() as session:
            row = session.get(SourceDocument, document_id)
            if row is None:
                raise KeyError(document_id)
            row.storage_path = _optional_text(archive.get("storage_path"))
            row.parsed_text_path = _optional_text(archive.get("parsed_text_path"))
            row.sha256 = _optional_text(archive.get("sha256"))
            row.size_bytes = _optional_int(archive.get("size_bytes"))
            metadata = json_loads(row.metadata_json, {})
            metadata.update(
                {
                    "archive_final_url": archive.get("final_url"),
                    "parse_warning": archive.get("parse_warning"),
                }
            )
            row.metadata_json = json_dumps(metadata)
            session.flush()
            return row

    def add_financial_fact(self, payload: Mapping[str, Any]) -> tuple[FinancialFact, bool]:
        identity = (
            str(payload["security_id"]),
            str(payload["metric_code"]),
            _required_date(payload["period_end"]),
            str(payload["source_name"]),
            str(payload["source_record_id"]),
            int(payload.get("revision_no") or 0),
            str(payload.get("transform_version") or "raw-v1"),
        )
        with self.database.session() as session:
            existing = session.scalar(
                select(FinancialFact).where(
                    FinancialFact.security_id == identity[0],
                    FinancialFact.metric_code == identity[1],
                    FinancialFact.period_end == identity[2],
                    FinancialFact.source_name == identity[3],
                    FinancialFact.source_record_id == identity[4],
                    FinancialFact.revision_no == identity[5],
                    FinancialFact.transform_version == identity[6],
                )
            )
            if existing is not None:
                return existing, False
            row = FinancialFact(
                id=new_id("ff"),
                security_id=identity[0],
                metric_code=identity[1],
                statement_type=str(payload.get("statement_type") or "indicator"),
                period_end=identity[2],
                announced_at=_required_datetime(payload["announced_at"]),
                available_at=_required_datetime(payload.get("available_at") or payload["announced_at"]),
                ingested_at=_required_datetime(payload.get("ingested_at") or utc_now_naive()),
                value=float(payload["value"]),
                unit=str(payload.get("unit") or "CNY"),
                currency=str(payload.get("currency") or "CNY"),
                scope=str(payload.get("scope") or "consolidated"),
                report_type=str(payload.get("report_type") or "periodic"),
                source_name=identity[3],
                source_record_id=identity[4],
                document_id=_optional_text(payload.get("document_id")),
                revision_no=identity[5],
                transform_version=identity[6],
                quality=str(payload.get("quality") or "reported"),
                raw_json=json_dumps(payload.get("raw") or {}),
            )
            session.add(row)
            session.flush()
            return row, True

    def add_market_price(self, payload: Mapping[str, Any]) -> tuple[MarketPriceBasis, bool]:
        security_id = str(payload["security_id"])
        trade_date = _required_date(payload["trade_date"])
        basis = str(payload.get("basis") or "raw")
        source_name = str(payload["source_name"])
        with self.database.session() as session:
            existing = session.scalar(
                select(MarketPriceBasis).where(
                    MarketPriceBasis.security_id == security_id,
                    MarketPriceBasis.trade_date == trade_date,
                    MarketPriceBasis.basis == basis,
                    MarketPriceBasis.source_name == source_name,
                )
            )
            if existing is not None:
                return existing, False
            row = MarketPriceBasis(
                id=new_id("px"),
                security_id=security_id,
                trade_date=trade_date,
                basis=basis,
                open=_optional_float(payload.get("open")),
                high=_optional_float(payload.get("high")),
                low=_optional_float(payload.get("low")),
                close=float(payload["close"]),
                volume=_optional_float(payload.get("volume")),
                amount=_optional_float(payload.get("amount")),
                adj_factor=_optional_float(payload.get("adj_factor")),
                currency=str(payload.get("currency") or "CNY"),
                source_name=source_name,
                available_at=_required_datetime(payload["available_at"]),
                ingested_at=_required_datetime(payload.get("ingested_at") or utc_now_naive()),
            )
            session.add(row)
            session.flush()
            return row, True

    def add_corporate_action(self, payload: Mapping[str, Any]) -> tuple[CorporateAction, bool]:
        source_name = str(payload["source_name"])
        source_record_id = str(payload["source_record_id"])
        with self.database.session() as session:
            existing = session.scalar(
                select(CorporateAction).where(
                    CorporateAction.source_name == source_name,
                    CorporateAction.source_record_id == source_record_id,
                )
            )
            if existing is not None:
                return existing, False
            row = CorporateAction(
                id=new_id("ca"),
                security_id=str(payload["security_id"]),
                action_type=str(payload["action_type"]),
                announced_at=_required_datetime(payload["announced_at"]),
                available_at=_required_datetime(payload.get("available_at") or payload["announced_at"]),
                record_date=_optional_date(payload.get("record_date")),
                ex_date=_optional_date(payload.get("ex_date")),
                effective_date=_optional_date(payload.get("effective_date")),
                amount_per_share=_optional_float(payload.get("amount_per_share")),
                ratio=_optional_float(payload.get("ratio")),
                currency=str(payload.get("currency") or "CNY"),
                source_name=source_name,
                source_record_id=source_record_id,
                document_id=_optional_text(payload.get("document_id")),
                metadata_json=json_dumps(payload.get("metadata") or {}),
            )
            session.add(row)
            session.flush()
            return row, True

    def financial_facts_as_of(self, security_id: str, as_of: datetime) -> list[FinancialFact]:
        """Return only the newest revision that was visible at ``as_of``."""
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(FinancialFact)
                    .where(
                        FinancialFact.security_id == security_id,
                        FinancialFact.available_at <= as_of,
                    )
                    .order_by(
                        FinancialFact.metric_code,
                        FinancialFact.period_end,
                        desc(FinancialFact.available_at),
                        desc(FinancialFact.revision_no),
                    )
                )
            )
            result: list[FinancialFact] = []
            seen: set[tuple[str, date, str, str]] = set()
            for row in rows:
                key = (row.metric_code, row.period_end, row.statement_type, row.scope)
                if key in seen:
                    continue
                seen.add(key)
                result.append(row)
            return result

    def documents_as_of(self, security_id: str, as_of: datetime, *, limit: int = 200) -> list[SourceDocument]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(SourceDocument)
                    .where(
                        SourceDocument.security_id == security_id,
                        SourceDocument.available_at <= as_of,
                    )
                    .order_by(desc(SourceDocument.published_at))
                    .limit(limit)
                )
            )

    def market_prices_as_of(
        self,
        security_id: str,
        as_of: datetime,
        *,
        basis: str = "raw",
        limit: int = 260,
    ) -> list[MarketPriceBasis]:
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(MarketPriceBasis)
                    .where(
                        MarketPriceBasis.security_id == security_id,
                        MarketPriceBasis.available_at <= as_of,
                        MarketPriceBasis.basis == basis,
                    )
                    .order_by(desc(MarketPriceBasis.trade_date))
                    .limit(limit)
                )
            )
            return list(reversed(rows))

    def corporate_actions_as_of(self, security_id: str, as_of: datetime) -> list[CorporateAction]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(CorporateAction)
                    .where(
                        CorporateAction.security_id == security_id,
                        CorporateAction.available_at <= as_of,
                    )
                    .order_by(CorporateAction.available_at)
                )
            )

    def published_reports_as_of(
        self,
        security_id: str,
        as_of: datetime,
        *,
        limit: int = 10,
    ) -> list[ResearchReport]:
        """Expose only human-approved prior research to a future Evidence Pack."""
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ResearchReport)
                    .where(
                        ResearchReport.security_id == security_id,
                        ResearchReport.status == "published",
                        ResearchReport.as_of <= as_of,
                        ResearchReport.published_at <= as_of,
                    )
                    .order_by(desc(ResearchReport.as_of), desc(ResearchReport.published_at))
                    .limit(max(1, min(int(limit), 50)))
                )
            )

    # Evidence packs ----------------------------------------------------------
    def save_evidence_pack(self, payload: Mapping[str, Any]) -> EvidencePackRecord:
        with self.database.session() as session:
            existing = session.get(EvidencePackRecord, str(payload["pack_id"]))
            if existing is not None:
                if existing.manifest_hash != payload["manifest_hash"]:
                    raise ValueError("evidence pack identity collision")
                return existing
            row = EvidencePackRecord(
                pack_id=str(payload["pack_id"]),
                security_id=str(payload["security_id"]),
                workflow=str(payload["workflow"]),
                as_of=_required_datetime(payload["as_of"]),
                data_cutoff=_required_datetime(payload["data_cutoff"]),
                schema_version=str(payload.get("schema_version") or "1.0"),
                manifest_path=str(payload["manifest_path"]),
                manifest_hash=str(payload["manifest_hash"]),
                quality_status=str(payload["quality_status"]),
                coverage_json=json_dumps(payload.get("coverage") or {}),
                warnings_json=json_dumps(payload.get("warnings") or []),
                blocking_gaps_json=json_dumps(payload.get("blocking_gaps") or []),
            )
            session.add(row)
            session.flush()
            return row

    def get_evidence_pack(self, pack_id: str) -> Optional[EvidencePackRecord]:
        with self.database.session() as session:
            return session.get(EvidencePackRecord, pack_id)

    # Persistent queue --------------------------------------------------------
    def create_job(self, payload: Mapping[str, Any]) -> tuple[ResearchJob, bool]:
        key = str(payload["idempotency_key"])
        with self.database.session() as session:
            existing = session.scalar(select(ResearchJob).where(ResearchJob.idempotency_key == key))
            if existing is not None:
                return existing, False
            row = ResearchJob(
                id=str(payload.get("id") or new_id("job")),
                security_id=str(payload["security_id"]),
                workflow=str(payload["workflow"]),
                workflow_version=str(payload["workflow_version"]),
                trigger_reason=str(payload.get("trigger_reason") or "manual"),
                source_event_id=_optional_text(payload.get("source_event_id")),
                status=str(payload.get("status") or "queued"),
                priority=int(payload.get("priority") or 100),
                idempotency_key=key,
                trace_id=str(payload.get("trace_id") or uuid.uuid4().hex),
                pack_id=_optional_text(payload.get("pack_id")),
                max_retries=int(payload.get("max_retries") if payload.get("max_retries") is not None else 2),
                metadata_json=json_dumps(payload.get("metadata") or {}),
            )
            session.add(row)
            session.flush()
            return row, True

    def get_job(self, job_id: str) -> Optional[ResearchJob]:
        with self.database.session() as session:
            return session.get(ResearchJob, job_id)

    def get_run(self, run_id: str) -> Optional[ResearchRun]:
        with self.database.session() as session:
            return session.get(ResearchRun, run_id)

    def token_usage_since(self, since: datetime) -> dict[str, int]:
        """Return non-overlapping Codex token counters for the requested UTC window."""
        with self.database.session() as session:
            row = session.execute(
                select(
                    func.coalesce(func.sum(ResearchRun.input_tokens), 0),
                    func.coalesce(func.sum(ResearchRun.output_tokens), 0),
                ).where(ResearchRun.started_at >= _required_datetime(since))
            ).one()
        input_tokens = max(0, int(row[0] or 0))
        output_tokens = max(0, int(row[1] or 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def get_document(self, document_id: str) -> Optional[SourceDocument]:
        with self.database.session() as session:
            return session.get(SourceDocument, document_id)

    def list_jobs(
        self,
        *,
        status: Optional[str] = None,
        security_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ResearchJob], int]:
        filters = []
        if status:
            filters.append(ResearchJob.status == status)
        if security_id:
            filters.append(ResearchJob.security_id == security_id)
        with self.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(ResearchJob).where(*filters)) or 0)
            rows = list(
                session.scalars(
                    select(ResearchJob)
                    .where(*filters)
                    .order_by(desc(ResearchJob.created_at))
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            return rows, total

    def set_job_pack(self, job_id: str, pack_id: str, *, quality_status: str) -> ResearchJob:
        target_status = "data_ready" if quality_status in {"ready", "degraded"} else "blocked_data"
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.status not in {"queued", "collecting_data", "blocked_data"}:
                raise ValueError(f"job cannot attach data from status {row.status}")
            row.pack_id = pack_id
            row.status = target_status
            row.updated_at = utc_now_naive()
            return row

    def mark_collecting_data(self, job_id: str) -> ResearchJob:
        return self._update_job_state(job_id, allowed={"queued", "blocked_data"}, status="collecting_data")

    def block_job(self, job_id: str, *, error_code: str, error_message: str) -> ResearchJob:
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.status not in {"queued", "collecting_data", "blocked_data"}:
                raise ValueError(f"job cannot be data-blocked from status {row.status}")
            row.status = "blocked_data"
            row.error_code = error_code[:64]
            row.error_message = error_message[:2000]
            row.updated_at = utc_now_naive()
            return row

    def recover_expired_leases(self, now: Optional[datetime] = None) -> int:
        now = now or utc_now_naive()
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(ResearchJob).where(
                        ResearchJob.status.in_({"analyzing", "validating"}),
                        ResearchJob.lease_expires_at.is_not(None),
                        ResearchJob.lease_expires_at < now,
                    )
                )
            )
            for row in rows:
                if row.cancel_requested_at is not None:
                    row.status = "cancelled"
                elif row.retry_count < row.max_retries:
                    row.status = "failed_retryable"
                    row.retry_count += 1
                else:
                    row.status = "failed_permanent"
                row.lease_owner = None
                row.lease_token = None
                row.lease_expires_at = None
                row.heartbeat_at = None
                row.error_code = "lease_expired"
                row.error_message = "Worker lease expired"
                row.finished_at = now if row.status in {"cancelled", "failed_permanent"} else None
                row.updated_at = now
            return len(rows)

    def claim_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> tuple[ResearchJob, ResearchRun] | None:
        now = now or utc_now_naive()
        self.recover_expired_leases(now)
        for _ in range(5):
            with self.database.session() as session:
                candidate = session.scalar(
                    select(ResearchJob)
                    .where(
                        ResearchJob.status.in_(CLAIMABLE_JOB_STATUSES),
                        ResearchJob.cancel_requested_at.is_(None),
                        ResearchJob.retry_count <= ResearchJob.max_retries,
                    )
                    .order_by(ResearchJob.priority, ResearchJob.created_at)
                    .limit(1)
                )
                if candidate is None:
                    return None
                token = uuid.uuid4().hex
                result = session.execute(
                    update(ResearchJob)
                    .where(
                        ResearchJob.id == candidate.id,
                        ResearchJob.status == candidate.status,
                        ResearchJob.lease_token.is_(None),
                    )
                    .values(
                        status="analyzing",
                        lease_owner=worker_id,
                        lease_token=token,
                        lease_expires_at=now + timedelta(seconds=max(30, lease_seconds)),
                        heartbeat_at=now,
                        started_at=func.coalesce(ResearchJob.started_at, now),
                        updated_at=now,
                        error_code=None,
                        error_message=None,
                    )
                )
                if not result.rowcount:
                    continue
                attempt_no = int(
                    session.scalar(
                        select(func.count()).select_from(ResearchRun).where(ResearchRun.job_id == candidate.id)
                    )
                    or 0
                ) + 1
                run = ResearchRun(
                    id=new_id("run"),
                    job_id=candidate.id,
                    attempt_no=attempt_no,
                    status="running",
                    worker_id=worker_id,
                    started_at=now,
                )
                session.add(run)
                session.flush()
                claimed = session.get(ResearchJob, candidate.id)
                # The bulk conditional UPDATE may expire the in-session ORM
                # state. Materialize both rows before the session closes so
                # the API can safely serialize the claimed lease snapshot.
                session.refresh(claimed)
                session.refresh(run)
                return claimed, run
        return None

    def heartbeat(
        self,
        job_id: str,
        *,
        lease_token: str,
        lease_seconds: int,
        now: Optional[datetime] = None,
    ) -> ResearchJob:
        now = now or utc_now_naive()
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.lease_token != lease_token or row.status not in {"analyzing", "validating"}:
                raise PermissionError("invalid or inactive research job lease")
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=max(30, lease_seconds))
            row.updated_at = now
            return row

    def request_cancel(self, job_id: str) -> ResearchJob:
        now = utc_now_naive()
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.status in NON_CANCELLABLE_JOB_STATUSES:
                return row
            row.cancel_requested_at = now
            if row.status in {"queued", "collecting_data", "data_ready", "blocked_data", "failed_retryable"}:
                row.status = "cancelled"
                row.finished_at = now
            # Running jobs keep their leased state so heartbeat can observe
            # the marker and the Worker can finish them as cancelled.
            row.updated_at = now
            return row

    def mark_validating(self, job_id: str, lease_token: str) -> ResearchJob:
        return self._leased_state(job_id, lease_token, allowed={"analyzing"}, status="validating")

    def complete_job(self, job_id: str, lease_token: str) -> ResearchJob:
        now = utc_now_naive()
        row = self._leased_state(job_id, lease_token, allowed={"validating"}, status="awaiting_review")
        with self.database.session() as session:
            current = session.get(ResearchJob, row.id)
            current.lease_owner = None
            current.lease_token = None
            current.lease_expires_at = None
            current.heartbeat_at = None
            current.finished_at = now
            current.updated_at = now
            return current

    def fail_job(
        self,
        job_id: str,
        lease_token: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
    ) -> ResearchJob:
        now = utc_now_naive()
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.lease_token != lease_token:
                raise PermissionError("invalid research job lease")
            if row.cancel_requested_at is not None:
                status = "cancelled"
            elif retryable and row.retry_count < row.max_retries:
                status = "failed_retryable"
                row.retry_count += 1
            else:
                status = "failed_permanent"
            row.status = status
            row.error_code = error_code[:64]
            row.error_message = error_message[:2000]
            row.lease_owner = None
            row.lease_token = None
            row.lease_expires_at = None
            row.heartbeat_at = None
            row.finished_at = now if status in {"cancelled", "failed_permanent"} else None
            row.updated_at = now
            return row

    def finish_run(self, run_id: str, payload: Mapping[str, Any]) -> ResearchRun:
        with self.database.session() as session:
            row = session.get(ResearchRun, run_id)
            if row is None:
                raise KeyError(run_id)
            for field in (
                "status",
                "model",
                "plugin_skill",
                "plugin_version",
                "workflow_version",
                "mcp_server_version",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "duration_seconds",
                "exit_code",
                "artifact_path",
                "error_code",
                "error_message",
            ):
                if field in payload:
                    setattr(row, field, payload[field])
            row.metadata_json = json_dumps(payload.get("metadata") or {})
            row.finished_at = _required_datetime(payload.get("finished_at") or utc_now_naive())
            return row

    # Reports -----------------------------------------------------------------
    def create_report(
        self,
        payload: Mapping[str, Any],
        evidence_refs: Iterable[Mapping[str, Any]],
    ) -> ResearchReport:
        structured = payload["structured"]
        encoded = json_dumps(structured)
        content_hash = f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
        with self.database.session() as session:
            return self._create_report_row(session, payload, evidence_refs, encoded, content_hash)

    def create_report_and_complete_job(
        self,
        payload: Mapping[str, Any],
        evidence_refs: Iterable[Mapping[str, Any]],
        *,
        lease_token: str,
    ) -> ResearchReport:
        """Atomically persist a validated report and release its job lease."""
        structured = payload["structured"]
        encoded = json_dumps(structured)
        content_hash = f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"
        now = utc_now_naive()
        with self.database.session() as session:
            job = session.get(ResearchJob, str(payload["job_id"]))
            if job is None:
                raise KeyError(payload["job_id"])
            if job.lease_token != lease_token:
                raise PermissionError("invalid research job lease")
            if job.cancel_requested_at is not None:
                raise RuntimeError("research job cancellation requested")
            if job.status != "validating":
                raise ValueError(f"invalid research job transition {job.status} -> awaiting_review")
            row = self._create_report_row(session, payload, evidence_refs, encoded, content_hash)
            job.status = "awaiting_review"
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.finished_at = now
            job.updated_at = now
            return row

    @staticmethod
    def _create_report_row(session, payload, evidence_refs, encoded, content_hash) -> ResearchReport:
        existing = session.scalar(select(ResearchReport).where(ResearchReport.run_id == payload["run_id"]))
        if existing is not None:
            if existing.content_sha256 != content_hash:
                raise ValueError("run already has a different research report")
            return existing
        parent = session.scalar(
            select(ResearchReport)
            .where(
                ResearchReport.security_id == payload["security_id"],
                ResearchReport.report_type == payload["report_type"],
            )
            .order_by(desc(ResearchReport.as_of), desc(ResearchReport.created_at))
            .limit(1)
        )
        row = ResearchReport(
            id=new_id("rpt"),
            job_id=str(payload["job_id"]),
            run_id=str(payload["run_id"]),
            security_id=str(payload["security_id"]),
            pack_id=str(payload["pack_id"]),
            parent_report_id=parent.id if parent else None,
            report_type=str(payload["report_type"]),
            as_of=_required_datetime(payload["as_of"]),
            status="awaiting_review",
            structured_json=encoded,
            markdown=str(payload["markdown"]),
            artifact_path=_optional_text(payload.get("artifact_path")),
            content_sha256=content_hash,
            model=_optional_text(payload.get("model")),
            plugin_version=_optional_text(payload.get("plugin_version")),
            workflow_version=_optional_text(payload.get("workflow_version")),
        )
        session.add(row)
        session.flush()
        seen: set[tuple[str, str, Optional[str]]] = set()
        for ref in evidence_refs:
            item = (
                str(ref.get("evidence_type") or "evidence"),
                str(ref["evidence_id"]),
                _optional_text(ref.get("citation_path")),
            )
            if item in seen:
                continue
            seen.add(item)
            session.add(
                ReportEvidence(
                    report_id=row.id,
                    evidence_type=item[0],
                    evidence_id=item[1],
                    citation_path=item[2],
                )
            )
        return row

    def list_reports(
        self,
        *,
        security_id: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ResearchReport], int]:
        filters = []
        if security_id:
            filters.append(ResearchReport.security_id == security_id)
        if status:
            filters.append(ResearchReport.status == status)
        with self.database.session() as session:
            total = int(session.scalar(select(func.count()).select_from(ResearchReport).where(*filters)) or 0)
            rows = list(
                session.scalars(
                    select(ResearchReport)
                    .where(*filters)
                    .order_by(desc(ResearchReport.as_of), desc(ResearchReport.created_at))
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            return rows, total

    def get_report(self, report_id: str) -> Optional[ResearchReport]:
        with self.database.session() as session:
            return session.get(ResearchReport, report_id)

    def report_evidence(self, report_id: str) -> list[ReportEvidence]:
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(ReportEvidence)
                    .where(ReportEvidence.report_id == report_id)
                    .order_by(ReportEvidence.id)
                )
            )

    def review_report(self, report_id: str, *, decision: str, note: Optional[str]) -> ResearchReport:
        now = utc_now_naive()
        status_map = {"approve": "published", "reject": "rejected", "request_changes": "changes_requested"}
        if decision not in status_map:
            raise ValueError("unsupported review decision")
        with self.database.session() as session:
            row = session.get(ResearchReport, report_id)
            if row is None:
                raise KeyError(report_id)
            if row.status not in {"awaiting_review", "changes_requested"}:
                raise ValueError(f"report cannot be reviewed from status {row.status}")
            row.status = status_map[decision]
            row.review_note = note
            row.reviewed_at = now
            if decision == "approve":
                row.published_at = now
                job = session.get(ResearchJob, row.job_id)
                if job is not None:
                    job.status = "published"
                    job.finished_at = now
                    job.updated_at = now
            return row

    def replace_report_tracking_items(self, report: ResearchReport, structured: Mapping[str, Any]) -> None:
        with self.database.session() as session:
            for item in structured.get("thesis") or []:
                if not isinstance(item, Mapping) or not item.get("statement"):
                    continue
                session.add(
                    ThesisItem(
                        id=new_id("thesis"),
                        security_id=report.security_id,
                        report_id=report.id,
                        statement=str(item["statement"]),
                        status=str(item.get("status") or "active"),
                        confidence=_optional_float(item.get("confidence")),
                        invalidation_condition=_optional_text(item.get("invalidation_condition")),
                        evidence_ids_json=json_dumps(item.get("evidence_ids") or []),
                    )
                )
            for item in structured.get("catalysts") or []:
                if not isinstance(item, Mapping) or not item.get("title"):
                    continue
                session.add(
                    Catalyst(
                        id=new_id("cat"),
                        security_id=report.security_id,
                        report_id=report.id,
                        title=str(item["title"]),
                        description=_optional_text(item.get("description")),
                        expected_at=_optional_datetime(item.get("expected_at")),
                        probability=_optional_float(item.get("probability")),
                        impact=_optional_text(item.get("impact")),
                        evidence_ids_json=json_dumps(item.get("evidence_ids") or []),
                    )
                )

    def security_timeline(self, security_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.session() as session:
            documents = list(
                session.scalars(
                    select(SourceDocument)
                    .where(SourceDocument.security_id == security_id)
                    .order_by(desc(SourceDocument.published_at))
                    .limit(limit)
                )
            )
            reports = list(
                session.scalars(
                    select(ResearchReport)
                    .where(ResearchReport.security_id == security_id)
                    .order_by(desc(ResearchReport.as_of))
                    .limit(limit)
                )
            )
            catalysts = list(
                session.scalars(
                    select(Catalyst)
                    .where(Catalyst.security_id == security_id)
                    .order_by(desc(Catalyst.expected_at))
                    .limit(limit)
                )
            )
        items = [
            {
                "type": "document",
                "id": row.id,
                "occurred_at": row.published_at,
                "title": row.title,
                "status": row.document_type,
            }
            for row in documents
        ]
        items.extend(
            {
                "type": "report",
                "id": row.id,
                "occurred_at": row.as_of,
                "title": row.report_type,
                "status": row.status,
            }
            for row in reports
        )
        items.extend(
            {
                "type": "catalyst",
                "id": row.id,
                "occurred_at": row.expected_at or row.created_at,
                "title": row.title,
                "status": row.status,
            }
            for row in catalysts
        )
        return sorted(items, key=lambda item: item["occurred_at"] or datetime.min, reverse=True)[:limit]

    def _update_job_state(self, job_id: str, *, allowed: set[str], status: str) -> ResearchJob:
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.status not in allowed:
                raise ValueError(f"invalid research job transition {row.status} -> {status}")
            row.status = status
            row.updated_at = utc_now_naive()
            return row

    def _leased_state(self, job_id: str, lease_token: str, *, allowed: set[str], status: str) -> ResearchJob:
        with self.database.session() as session:
            row = session.get(ResearchJob, job_id)
            if row is None:
                raise KeyError(job_id)
            if row.lease_token != lease_token:
                raise PermissionError("invalid research job lease")
            if row.cancel_requested_at is not None or row.status == "cancel_requested":
                raise RuntimeError("research job cancellation requested")
            if row.status not in allowed:
                raise ValueError(f"invalid research job transition {row.status} -> {status}")
            row.status = status
            row.updated_at = utc_now_naive()
            return row


def _exchange_from_ts_code(ts_code: str) -> str:
    suffix = ts_code.rsplit(".", 1)[-1] if "." in ts_code else ""
    return {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix, suffix or "UNKNOWN")


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    return float(value)


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    return int(value)


def _required_datetime(value: Any) -> datetime:
    parsed = _optional_datetime(value)
    if parsed is None:
        raise ValueError("datetime value is required")
    return parsed


def _optional_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None and value.utcoffset() is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _required_date(value: Any) -> date:
    parsed = _optional_date(value)
    if parsed is None:
        raise ValueError("date value is required")
    return parsed


def _optional_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return date.fromisoformat(text)
