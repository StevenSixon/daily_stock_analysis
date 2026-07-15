from __future__ import annotations

from src.services.research_trigger_service import ResearchDisclosureScanner, ResearchTriggerService


class _FakeResearchService:
    def __init__(self) -> None:
        self.calls = []

    def create_job(self, **kwargs):
        self.calls.append(kwargs)
        return {"id": "job-test", "workflow": kwargs["workflow"]}, len(self.calls) == 1


def test_disclosure_trigger_maps_periodic_and_ignores_minor(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_AUTO_TRIGGER_DISCLOSURES", "true")
    fake = _FakeResearchService()
    service = ResearchTriggerService(fake)
    result = service.on_disclosure(
        security_code="600519.SH",
        document={
            "external_id": "annual-2025",
            "document_type": "annual_report",
            "title": "2025年年度报告",
        },
    )
    assert result["status"] == "created"
    assert fake.calls[0]["workflow"] == "earnings_deep_dive"
    assert fake.calls[0]["source_event_id"] == "disclosure:annual-2025"
    ignored = service.on_disclosure(
        security_code="600519.SH",
        document={"external_id": "minor", "document_type": "announcement", "title": "一般提示公告"},
    )
    assert ignored == {"status": "ignored", "reason": "non_material_disclosure"}


def test_alert_trigger_is_separately_opt_in(monkeypatch) -> None:
    fake = _FakeResearchService()
    service = ResearchTriggerService(fake)
    monkeypatch.setenv("RESEARCH_AUTO_TRIGGER_ALERTS", "false")
    assert service.on_alert(security_code="600519", trigger_id=42)["status"] == "disabled"
    monkeypatch.setenv("RESEARCH_AUTO_TRIGGER_ALERTS", "true")
    result = service.on_alert(security_code="600519", trigger_id=42)
    assert result["status"] == "created"
    assert fake.calls[0]["workflow"] == "long_short_pitch"
    assert fake.calls[0]["source_event_id"] == "alert:42"


def test_disclosure_scanner_is_deduplicated_and_failure_contained() -> None:
    class FakeScannerService:
        def refresh_disclosures(self, code, *, lookback_days):
            assert lookback_days == 30
            if code == "BAD":
                raise RuntimeError("fixture failure")
            return {
                "disclosures": {"discovered": 2, "created": 1},
                "research_triggers": [
                    {"status": "created"},
                    {"status": "deduplicated"},
                ],
            }

    stats = ResearchDisclosureScanner(FakeScannerService()).run_once(
        ["600519", "600519", "bad"],
        lookback_days=30,
    )
    assert stats == {
        "securities": 2,
        "refreshed": 1,
        "failed": 1,
        "discovered": 2,
        "created": 1,
        "research_jobs": 1,
    }
