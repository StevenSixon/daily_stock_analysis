from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middlewares.auth import add_auth_middleware
from api.v1.endpoints.research import router
from src.services.research_service import ResearchConflictError, get_research_service, reset_research_service


REPORT_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "integrations"
    / "codex"
    / "fixtures"
    / "earnings_deep_dive_600519_report.json"
)


def _seed_ready_data(service) -> None:
    security = service.repository.upsert_security(
        {
            "ts_code": "600519.SH",
            "symbol": "600519",
            "exchange": "SSE",
            "name": "贵州茅台",
            "industry": "白酒",
        }
    )
    service.repository.add_document(
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
        service.repository.add_financial_fact(
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
    service.repository.add_market_price(
        {
            "security_id": security.id,
            "trade_date": "2026-07-14",
            "basis": "raw",
            "close": 1500.0,
            "source_name": "tushare",
            "available_at": "2026-07-14T08:00:00Z",
        }
    )


def _bind_evidence(value, evidence_id: str):
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if key == "evidence_id":
                value[key] = evidence_id
            elif key == "evidence_ids" and isinstance(item, list):
                value[key] = [evidence_id]
            else:
                _bind_evidence(item, evidence_id)
    elif isinstance(value, list):
        for item in value:
            _bind_evidence(item, evidence_id)


def test_research_api_job_worker_report_lifecycle(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_ENABLED", "true")
    monkeypatch.setenv("ADMIN_AUTH_ENABLED", "false")
    monkeypatch.setenv("RESEARCH_WORKER_TOKEN", "w" * 48)
    monkeypatch.setenv("RESEARCH_DATABASE_PATH", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_EVIDENCE_PACKS_DIR", str(tmp_path / "packs"))
    monkeypatch.setenv("RESEARCH_DOCUMENTS_DIR", str(tmp_path / "documents"))
    reset_research_service()
    service = get_research_service()
    _seed_ready_data(service)

    app = FastAPI()
    app.include_router(router, prefix="/api/v1/research")
    add_auth_middleware(app)
    client = TestClient(app)

    status = client.get("/api/v1/research/status")
    assert status.status_code == 200
    assert status.json()["database_ready"] is True

    created = client.post(
        "/api/v1/research/jobs",
        json={
            "security_code": "600519",
            "workflow": "earnings_deep_dive",
            "as_of": "2026-07-15T10:00:00Z",
        },
    )
    assert created.status_code == 202, created.text
    job = created.json()["job"]
    assert job["status"] == "data_ready"
    assert job["pack_id"].startswith("ep_")

    assert client.post("/api/v1/research/worker/claim", json={"worker_id": "test"}).status_code == 401
    headers = {"Authorization": f"Bearer {'w' * 48}"}
    claimed = client.post(
        "/api/v1/research/worker/claim",
        headers=headers,
        json={"worker_id": "test-worker", "lease_seconds": 300},
    )
    assert claimed.status_code == 200, claimed.text
    assignment = claimed.json()["assignment"]
    assert assignment["job"]["id"] == job["id"]

    manifest = client.get(
        f"/api/v1/research/worker/packs/{job['pack_id']}",
        headers=headers,
    )
    assert manifest.status_code == 200
    evidence_id = manifest.json()["evidence_manifest"][0]["evidence_id"]

    report = copy.deepcopy(json.loads(REPORT_FIXTURE.read_text(encoding="utf-8")))
    report["as_of"] = assignment["pack"]["as_of"]
    _bind_evidence(report, evidence_id)
    completed = client.post(
        f"/api/v1/research/worker/jobs/{job['id']}/complete",
        headers=headers,
        json={
            "run_id": assignment["run_id"],
            "lease_token": assignment["lease_token"],
            "report": report,
            "run_metadata": {
                "model": "fixture-model",
                "plugin_skill": "public-equity-investing",
                "plugin_version": "fixture-v1",
                "workflow_version": "earnings_deep_dive-v1",
                "mcp_server_version": "research-v1",
                "usage": {"input_tokens": 10, "output_tokens": 20},
                "exit_code": 0,
            },
        },
    )
    assert completed.status_code == 200, completed.text
    research_report = completed.json()
    assert research_report["status"] == "awaiting_review"

    evidence = client.get(f"/api/v1/research/reports/{research_report['id']}/evidence")
    assert evidence.status_code == 200
    assert {item["evidence_id"] for item in evidence.json()["items"]} == {evidence_id}
    assert evidence.json()["items"][0]["evidence_type"] in {
        item["evidence_type"] for item in manifest.json()["evidence_manifest"]
    }
    approved = client.post(
        f"/api/v1/research/reports/{research_report['id']}/review",
        json={"decision": "approve", "note": "fixture review"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "published"
    assert client.get(f"/api/v1/research/jobs/{job['id']}").json()["status"] == "published"
    follow_up_pack = service.evidence_builder.build(
        security_identifier="600519",
        workflow="thesis_update",
        as_of=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    assert follow_up_pack["previous_research"][0]["report_id"] == research_report["id"]
    assert any(
        item["evidence_id"] == f"report:{research_report['id']}"
        for item in follow_up_pack["evidence_manifest"]
    )

    service.database.close()
    reset_research_service()


def test_research_status_is_safe_while_disabled(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "should-not-exist.db"
    monkeypatch.setenv("RESEARCH_ENABLED", "false")
    monkeypatch.setenv("RESEARCH_DATABASE_PATH", str(database_path))
    reset_research_service()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/research")
    response = TestClient(app).get("/api/v1/research/status")
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert not database_path.exists()


def test_server_rejects_over_budget_report_and_blocks_later_monthly_claims(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_ENABLED", "true")
    monkeypatch.setenv("RESEARCH_DATABASE_PATH", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_EVIDENCE_PACKS_DIR", str(tmp_path / "packs"))
    monkeypatch.setenv("RESEARCH_RUN_TOKEN_BUDGET", "25")
    monkeypatch.delenv("RESEARCH_MONTHLY_TOKEN_BUDGET", raising=False)
    reset_research_service()
    service = get_research_service()
    _seed_ready_data(service)
    job, created = service.create_job(
        security_code="600519",
        workflow="earnings_deep_dive",
        as_of=datetime(2026, 7, 15, 10, tzinfo=timezone.utc),
    )
    assert created is True
    assignment = service.claim_job(worker_id="budget-worker", lease_seconds=300)
    manifest = service.evidence_builder.load(job["pack_id"])
    evidence_id = manifest["evidence_manifest"][0]["evidence_id"]
    report = copy.deepcopy(json.loads(REPORT_FIXTURE.read_text(encoding="utf-8")))
    report["as_of"] = assignment["pack"]["as_of"]
    _bind_evidence(report, evidence_id)

    with pytest.raises(ResearchConflictError, match="run token budget exceeded"):
        service.complete_worker_run(
            job["id"],
            run_id=assignment["run_id"],
            lease_token=assignment["lease_token"],
            report=report,
            run_metadata={"usage": {"input_tokens": 10, "output_tokens": 20}},
            artifact_path=None,
        )
    failed = service.get_job(job["id"])
    assert failed["status"] == "failed_permanent"
    assert failed["error_code"] == "token_budget_exceeded"
    assert service.repository.get_run(assignment["run_id"]).input_tokens == 10
    assert service.status()["current_month_tokens"] == 30
    assert service.list_reports()["total"] == 0

    monkeypatch.setenv("RESEARCH_RUN_TOKEN_BUDGET", "0")
    monkeypatch.setenv("RESEARCH_MONTHLY_TOKEN_BUDGET", "20")
    second, _ = service.create_job(
        security_code="600519",
        workflow="thesis_update",
        as_of=datetime(2026, 7, 15, 11, tzinfo=timezone.utc),
    )
    assert second["status"] == "data_ready"
    assert service.claim_job(worker_id="budget-worker", lease_seconds=300) is None
    service.database.close()
    reset_research_service()
