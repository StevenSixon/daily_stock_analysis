# -*- coding: utf-8 -*-
"""SQLAlchemy models for the independent research database."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base


ResearchBase = declarative_base()


def utc_now_naive() -> datetime:
    """Return a SQLite-friendly UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ResearchSchemaMigration(ResearchBase):
    __tablename__ = "research_schema_migrations"

    version = Column(String(64), primary_key=True)
    description = Column(String(255), nullable=False)
    applied_at = Column(DateTime, nullable=False, default=utc_now_naive)


class SecurityMaster(ResearchBase):
    __tablename__ = "security_master"

    id = Column(String(64), primary_key=True)
    ts_code = Column(String(32), nullable=False, unique=True, index=True)
    symbol = Column(String(24), nullable=False, index=True)
    exchange = Column(String(16), nullable=False, index=True)
    market = Column(String(16), nullable=False, default="cn", index=True)
    name = Column(String(128), nullable=False)
    industry = Column(String(128))
    currency = Column(String(8), nullable=False, default="CNY")
    list_status = Column(String(16), nullable=False, default="listed", index=True)
    listed_at = Column(Date)
    delisted_at = Column(Date)
    profile_json = Column(Text)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        UniqueConstraint("exchange", "symbol", name="uq_research_security_exchange_symbol"),
    )


class SourceDocument(ResearchBase):
    __tablename__ = "source_document"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    source_name = Column(String(32), nullable=False)
    external_id = Column(String(160), nullable=False)
    document_type = Column(String(48), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    published_at = Column(DateTime, nullable=False, index=True)
    available_at = Column(DateTime, nullable=False, index=True)
    period_end = Column(Date, index=True)
    url = Column(String(1200), nullable=False)
    storage_path = Column(String(1000))
    parsed_text_path = Column(String(1000))
    sha256 = Column(String(71))
    size_bytes = Column(Integer)
    revision_of_id = Column(String(64), ForeignKey("source_document.id", ondelete="SET NULL"))
    metadata_json = Column(Text)
    ingested_at = Column(DateTime, nullable=False, default=utc_now_naive, index=True)

    __table_args__ = (
        UniqueConstraint("source_name", "external_id", name="uq_research_document_source_external"),
        Index("ix_research_document_security_published", "security_id", "published_at"),
    )


class FinancialFact(ResearchBase):
    __tablename__ = "financial_fact"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    metric_code = Column(String(96), nullable=False)
    statement_type = Column(String(32), nullable=False)
    period_end = Column(Date, nullable=False)
    announced_at = Column(DateTime, nullable=False)
    available_at = Column(DateTime, nullable=False)
    ingested_at = Column(DateTime, nullable=False, default=utc_now_naive)
    value = Column(Float, nullable=False)
    unit = Column(String(24), nullable=False)
    currency = Column(String(8), nullable=False, default="CNY")
    scope = Column(String(24), nullable=False, default="consolidated")
    report_type = Column(String(32), nullable=False)
    source_name = Column(String(32), nullable=False)
    source_record_id = Column(String(192), nullable=False)
    document_id = Column(String(64), ForeignKey("source_document.id", ondelete="SET NULL"))
    revision_no = Column(Integer, nullable=False, default=0)
    transform_version = Column(String(64), nullable=False, default="raw-v1")
    quality = Column(String(24), nullable=False, default="reported")
    raw_json = Column(Text)

    __table_args__ = (
        UniqueConstraint(
            "security_id",
            "metric_code",
            "period_end",
            "source_name",
            "source_record_id",
            "revision_no",
            "transform_version",
            name="uq_research_financial_fact_revision",
        ),
        Index(
            "ix_research_financial_fact_pit",
            "security_id",
            "metric_code",
            "period_end",
            "available_at",
        ),
    )


class CorporateAction(ResearchBase):
    __tablename__ = "corporate_action"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    action_type = Column(String(48), nullable=False, index=True)
    announced_at = Column(DateTime, nullable=False)
    available_at = Column(DateTime, nullable=False, index=True)
    record_date = Column(Date)
    ex_date = Column(Date)
    effective_date = Column(Date)
    amount_per_share = Column(Float)
    ratio = Column(Float)
    currency = Column(String(8), default="CNY")
    source_name = Column(String(32), nullable=False)
    source_record_id = Column(String(192), nullable=False)
    document_id = Column(String(64), ForeignKey("source_document.id", ondelete="SET NULL"))
    metadata_json = Column(Text)
    ingested_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        UniqueConstraint("source_name", "source_record_id", name="uq_research_action_source_record"),
        Index("ix_research_action_security_available", "security_id", "available_at"),
    )


class MarketPriceBasis(ResearchBase):
    __tablename__ = "market_price_basis"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    trade_date = Column(Date, nullable=False)
    basis = Column(String(16), nullable=False, default="raw")
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float, nullable=False)
    volume = Column(Float)
    amount = Column(Float)
    adj_factor = Column(Float)
    currency = Column(String(8), nullable=False, default="CNY")
    source_name = Column(String(32), nullable=False)
    available_at = Column(DateTime, nullable=False, index=True)
    ingested_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        UniqueConstraint(
            "security_id", "trade_date", "basis", "source_name", name="uq_research_price_basis"
        ),
        Index("ix_research_price_security_date_basis", "security_id", "trade_date", "basis"),
    )


class EvidencePackRecord(ResearchBase):
    __tablename__ = "evidence_pack"

    pack_id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    workflow = Column(String(64), nullable=False)
    as_of = Column(DateTime, nullable=False, index=True)
    data_cutoff = Column(DateTime, nullable=False)
    schema_version = Column(String(16), nullable=False)
    manifest_path = Column(String(1000), nullable=False)
    manifest_hash = Column(String(71), nullable=False, unique=True)
    quality_status = Column(String(32), nullable=False, index=True)
    coverage_json = Column(Text, nullable=False)
    warnings_json = Column(Text, nullable=False)
    blocking_gaps_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        Index("ix_research_pack_security_workflow_asof", "security_id", "workflow", "as_of"),
    )


class ResearchJob(ResearchBase):
    __tablename__ = "research_job"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    workflow = Column(String(64), nullable=False)
    workflow_version = Column(String(64), nullable=False)
    trigger_reason = Column(String(64), nullable=False)
    source_event_id = Column(String(192))
    status = Column(String(32), nullable=False, default="queued", index=True)
    priority = Column(Integer, nullable=False, default=100)
    idempotency_key = Column(String(255), nullable=False, unique=True)
    trace_id = Column(String(64), nullable=False, index=True)
    pack_id = Column(String(64), ForeignKey("evidence_pack.pack_id", ondelete="SET NULL"))
    retry_count = Column(Integer, nullable=False, default=0)
    max_retries = Column(Integer, nullable=False, default=2)
    lease_owner = Column(String(128))
    lease_token = Column(String(128))
    lease_expires_at = Column(DateTime, index=True)
    heartbeat_at = Column(DateTime)
    cancel_requested_at = Column(DateTime)
    error_code = Column(String(64))
    error_message = Column(Text)
    metadata_json = Column(Text)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive, index=True)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    __table_args__ = (
        Index("ix_research_job_claim", "status", "priority", "created_at"),
    )


class ResearchRun(ResearchBase):
    __tablename__ = "research_run"

    id = Column(String(64), primary_key=True)
    job_id = Column(String(64), ForeignKey("research_job.id", ondelete="CASCADE"), nullable=False)
    attempt_no = Column(Integer, nullable=False)
    status = Column(String(32), nullable=False, default="running", index=True)
    worker_id = Column(String(128), nullable=False)
    model = Column(String(160))
    plugin_skill = Column(String(96))
    plugin_version = Column(String(64))
    workflow_version = Column(String(64))
    mcp_server_version = Column(String(64))
    input_tokens = Column(Integer)
    cached_input_tokens = Column(Integer)
    output_tokens = Column(Integer)
    reasoning_tokens = Column(Integer)
    duration_seconds = Column(Float)
    exit_code = Column(Integer)
    artifact_path = Column(String(1000))
    metadata_json = Column(Text)
    error_code = Column(String(64))
    error_message = Column(Text)
    started_at = Column(DateTime, nullable=False, default=utc_now_naive)
    finished_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_no", name="uq_research_run_attempt"),
        Index("ix_research_run_job_started", "job_id", "started_at"),
    )


class ResearchReport(ResearchBase):
    __tablename__ = "research_report"

    id = Column(String(64), primary_key=True)
    job_id = Column(String(64), ForeignKey("research_job.id", ondelete="RESTRICT"), nullable=False)
    run_id = Column(String(64), ForeignKey("research_run.id", ondelete="RESTRICT"), nullable=False)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    pack_id = Column(String(64), ForeignKey("evidence_pack.pack_id", ondelete="RESTRICT"), nullable=False)
    parent_report_id = Column(String(64), ForeignKey("research_report.id", ondelete="SET NULL"))
    report_type = Column(String(64), nullable=False)
    as_of = Column(DateTime, nullable=False, index=True)
    status = Column(String(32), nullable=False, default="awaiting_review", index=True)
    structured_json = Column(Text, nullable=False)
    markdown = Column(Text, nullable=False)
    artifact_path = Column(String(1000))
    content_sha256 = Column(String(71), nullable=False)
    model = Column(String(160))
    plugin_version = Column(String(64))
    workflow_version = Column(String(64))
    review_note = Column(Text)
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    reviewed_at = Column(DateTime)
    published_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("run_id", name="uq_research_report_run"),
        Index("ix_research_report_security_type_asof", "security_id", "report_type", "as_of"),
    )


class ReportEvidence(ResearchBase):
    __tablename__ = "report_evidence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    report_id = Column(String(64), ForeignKey("research_report.id", ondelete="CASCADE"), nullable=False)
    evidence_type = Column(String(32), nullable=False)
    evidence_id = Column(String(128), nullable=False)
    citation_path = Column(String(255))
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)

    __table_args__ = (
        UniqueConstraint(
            "report_id", "evidence_type", "evidence_id", "citation_path", name="uq_report_evidence_ref"
        ),
        Index("ix_report_evidence_lookup", "report_id", "evidence_type", "evidence_id"),
    )


class ThesisItem(ResearchBase):
    __tablename__ = "thesis_item"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    report_id = Column(String(64), ForeignKey("research_report.id", ondelete="SET NULL"))
    statement = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="active", index=True)
    confidence = Column(Float)
    invalidation_condition = Column(Text)
    next_check_at = Column(DateTime)
    evidence_ids_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        Index("ix_research_thesis_security_status", "security_id", "status"),
    )


class Catalyst(ResearchBase):
    __tablename__ = "catalyst"

    id = Column(String(64), primary_key=True)
    security_id = Column(String(64), ForeignKey("security_master.id", ondelete="RESTRICT"), nullable=False)
    report_id = Column(String(64), ForeignKey("research_report.id", ondelete="SET NULL"))
    title = Column(String(300), nullable=False)
    description = Column(Text)
    expected_at = Column(DateTime, index=True)
    probability = Column(Float)
    impact = Column(String(24))
    status = Column(String(24), nullable=False, default="planned", index=True)
    evidence_ids_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, nullable=False, default=utc_now_naive)
    updated_at = Column(DateTime, nullable=False, default=utc_now_naive, onupdate=utc_now_naive)

    __table_args__ = (
        Index("ix_research_catalyst_security_date", "security_id", "expected_at"),
    )
