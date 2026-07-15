from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect

from src.research.database import RESEARCH_SCHEMA_VERSION, ResearchDatabase
from src.research.evidence_pack import EvidencePackBuilder, calculate_pack_hash
from src.research.quality_gate import ResearchQualityGate
from src.research.repositories import ResearchRepository


def _repository() -> tuple[ResearchDatabase, ResearchRepository]:
    database = ResearchDatabase(db_url="sqlite:///:memory:")
    return database, ResearchRepository(database)


def _seed_ready_earnings_data(repo: ResearchRepository) -> str:
    security = repo.upsert_security(
        {
            "ts_code": "600519.SH",
            "symbol": "600519",
            "exchange": "SSE",
            "name": "贵州茅台",
            "industry": "白酒",
        }
    )
    repo.add_document(
        {
            "security_id": security.id,
            "source_name": "cninfo",
            "external_id": "annual-2025",
            "document_type": "annual_report",
            "title": "2025年年度报告",
            "published_at": "2026-03-28T10:00:00Z",
            "available_at": "2026-03-28T10:00:00Z",
            "period_end": "2025-12-31",
            "url": "https://static.cninfo.com.cn/example.pdf",
        }
    )
    for period, value, available in (
        ("2024-12-31", 100.0, "2025-03-28T10:00:00Z"),
        ("2025-12-31", 120.0, "2026-03-28T10:00:00Z"),
    ):
        repo.add_financial_fact(
            {
                "security_id": security.id,
                "metric_code": "revenue",
                "statement_type": "income",
                "period_end": period,
                "announced_at": available,
                "available_at": available,
                "value": value,
                "unit": "CNY",
                "source_name": "tushare",
                "source_record_id": f"revenue-{period}",
                "report_type": "annual",
            }
        )
    repo.add_market_price(
        {
            "security_id": security.id,
            "trade_date": "2026-03-27",
            "basis": "raw",
            "close": 1500.0,
            "source_name": "tushare",
            "available_at": "2026-03-27T08:00:00Z",
        }
    )
    return security.id


def test_research_database_is_independent_and_creates_expected_indexes():
    database, _repo = _repository()
    inspector = inspect(database.engine)
    tables = set(inspector.get_table_names())
    assert {
        "research_schema_migrations",
        "security_master",
        "source_document",
        "financial_fact",
        "market_price_basis",
        "evidence_pack",
        "research_job",
        "research_run",
        "research_report",
        "report_evidence",
        "thesis_item",
        "catalyst",
    } <= tables
    migration = database.engine.connect().exec_driver_sql(
        "SELECT version FROM research_schema_migrations"
    ).scalar_one()
    assert migration == RESEARCH_SCHEMA_VERSION
    fact_indexes = {item["name"] for item in inspector.get_indexes("financial_fact")}
    assert "ix_research_financial_fact_pit" in fact_indexes
    database.close()


def test_financial_fact_query_is_point_in_time_and_append_only():
    database, repo = _repository()
    security_id = _seed_ready_earnings_data(repo)
    old, _ = repo.add_financial_fact(
        {
            "security_id": security_id,
            "metric_code": "net_income",
            "statement_type": "income",
            "period_end": "2025-12-31",
            "announced_at": "2026-03-28T10:00:00Z",
            "available_at": "2026-03-28T10:00:00Z",
            "value": 50.0,
            "unit": "CNY",
            "source_name": "tushare",
            "source_record_id": "net-income-2025",
            "revision_no": 0,
            "report_type": "annual",
        }
    )
    revised, _ = repo.add_financial_fact(
        {
            "security_id": security_id,
            "metric_code": "net_income",
            "statement_type": "income",
            "period_end": "2025-12-31",
            "announced_at": "2026-04-10T10:00:00Z",
            "available_at": "2026-04-10T10:00:00Z",
            "value": 48.0,
            "unit": "CNY",
            "source_name": "tushare",
            "source_record_id": "net-income-2025",
            "revision_no": 1,
            "report_type": "annual",
        }
    )
    before = repo.financial_facts_as_of(
        security_id,
        datetime(2026, 4, 1),
    )
    after = repo.financial_facts_as_of(
        security_id,
        datetime(2026, 4, 11),
    )
    assert next(item for item in before if item.metric_code == "net_income").id == old.id
    assert next(item for item in after if item.metric_code == "net_income").id == revised.id
    database.close()


def test_evidence_pack_is_replayable_and_excludes_future_revisions(tmp_path):
    database, repo = _repository()
    security_id = _seed_ready_earnings_data(repo)
    repo.add_financial_fact(
        {
            "security_id": security_id,
            "metric_code": "future_metric",
            "statement_type": "indicator",
            "period_end": "2025-12-31",
            "announced_at": "2026-08-01T10:00:00Z",
            "available_at": "2026-08-01T10:00:00Z",
            "value": 1.0,
            "unit": "ratio",
            "source_name": "tushare",
            "source_record_id": "future",
            "report_type": "annual",
        }
    )
    builder = EvidencePackBuilder(
        repo,
        pack_root=tmp_path / "packs",
        documents_root=tmp_path / "documents",
    )
    as_of = datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc)
    first = builder.build(
        security_identifier="600519",
        workflow="earnings_deep_dive",
        as_of=as_of,
    )
    second = builder.build(
        security_identifier="600519.SH",
        workflow="earnings_deep_dive",
        as_of=as_of,
    )
    assert first == second
    assert first["quality"]["status"] == "ready"
    assert "future_metric" not in {item["metric_code"] for item in first["financials"]["facts"]}
    assert calculate_pack_hash(first) == first["manifest_hash"]
    assert builder.load(first["pack_id"]) == first
    assert (tmp_path / "packs" / f"{first['pack_id']}.json").stat().st_mode & 0o777 == 0o600
    database.close()


def test_quality_gate_blocks_missing_workflow_evidence():
    result = ResearchQualityGate().evaluate(
        workflow="initiating_coverage",
        financial_facts=[],
        filings=[],
        prices=[],
        corporate_actions=[],
    )
    assert result.status == "blocked_data"
    assert "three_historical_periods_required" in result.blocking_gaps
    assert any(item.startswith("complete_statements_required:") for item in result.blocking_gaps)


def _claimable_job(repo: ResearchRepository, security_id: str, *, max_retries: int = 2):
    row, _ = repo.create_job(
        {
            "security_id": security_id,
            "workflow": "earnings_deep_dive",
            "workflow_version": "v1",
            "trigger_reason": "test",
            "idempotency_key": f"lease-test-{max_retries}",
            "max_retries": max_retries,
        }
    )
    with repo.database.session() as session:
        current = session.get(type(row), row.id)
        current.status = "data_ready"
        current.pack_id = None
    return row


def test_running_cancel_remains_heartbeat_visible_and_recovers_as_cancelled():
    database, repo = _repository()
    security_id = _seed_ready_earnings_data(repo)
    job = _claimable_job(repo, security_id)
    claimed, _run = repo.claim_job(worker_id="worker", lease_seconds=30, now=datetime(2026, 7, 15, 10))
    cancelled = repo.request_cancel(job.id)
    assert cancelled.status == "analyzing"
    assert cancelled.cancel_requested_at is not None
    heartbeat = repo.heartbeat(
        job.id,
        lease_token=claimed.lease_token,
        lease_seconds=30,
        now=datetime(2026, 7, 15, 10, 0, 10),
    )
    assert heartbeat.cancel_requested_at is not None
    assert repo.recover_expired_leases(datetime(2026, 7, 15, 10, 1)) == 1
    assert repo.get_job(job.id).status == "cancelled"
    database.close()


def test_expired_final_lease_becomes_permanent_failure():
    database, repo = _repository()
    security_id = _seed_ready_earnings_data(repo)
    job = _claimable_job(repo, security_id, max_retries=0)
    repo.claim_job(worker_id="worker", lease_seconds=30, now=datetime(2026, 7, 15, 10))
    assert repo.recover_expired_leases(datetime(2026, 7, 15, 10, 1)) == 1
    recovered = repo.get_job(job.id)
    assert recovered.status == "failed_permanent"
    assert recovered.finished_at is not None
    database.close()


def test_report_finalize_refuses_cancelled_lease_without_partial_report():
    database, repo = _repository()
    security_id = _seed_ready_earnings_data(repo)
    job = _claimable_job(repo, security_id)
    claimed, run = repo.claim_job(worker_id="worker", lease_seconds=30)
    repo.mark_validating(job.id, claimed.lease_token)
    repo.request_cancel(job.id)
    with pytest.raises(RuntimeError, match="cancellation requested"):
        repo.create_report_and_complete_job(
            {
                "job_id": job.id,
                "run_id": run.id,
                "security_id": security_id,
                "pack_id": "ep-not-persisted",
                "report_type": "earnings_deep_dive",
                "as_of": datetime(2026, 7, 15),
                "structured": {"markdown": "test"},
                "markdown": "test",
            },
            [],
            lease_token=claimed.lease_token,
        )
    reports, total = repo.list_reports()
    assert reports == []
    assert total == 0
    database.close()
