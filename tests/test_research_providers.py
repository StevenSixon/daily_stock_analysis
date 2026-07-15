from __future__ import annotations

from pathlib import Path

import pytest

from src.research.providers.official_disclosure import (
    OfficialDisclosureError,
    OfficialDisclosureProvider,
)
from src.research.providers.tushare_research import TushareResearchProvider
from src.research.database import ResearchDatabase
from src.research.ingestion import ResearchIngestionService
from src.research.repositories import ResearchRepository


class FakeTushareClient:
    def stock_basic(self, **_kwargs):
        return [
            {
                "ts_code": "600519.SH",
                "symbol": "600519",
                "name": "贵州茅台",
                "industry": "白酒",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "20010827",
            }
        ]

    def _financial(self):
        return [
            {
                "ts_code": "600519.SH",
                "end_date": "20251231",
                "ann_date": "20260328",
                "f_ann_date": "20260328",
                "report_type": "1",
                "update_flag": "0",
                "revenue": 100.0,
                "total_assets": 200.0,
                "n_cashflow_act": 80.0,
                "roe": 25.0,
            }
        ]

    def income(self, **_kwargs):
        return self._financial()

    def balancesheet(self, **_kwargs):
        return self._financial()

    def cashflow(self, **_kwargs):
        return self._financial()

    def fina_indicator(self, **_kwargs):
        return self._financial()

    def dividend(self, **_kwargs):
        return []

    def daily(self, **_kwargs):
        return [
            {
                "trade_date": "20260714",
                "open": 1400,
                "high": 1500,
                "low": 1380,
                "close": 1480,
                "vol": 10,
                "amount": 100,
            }
        ]

    def adj_factor(self, **_kwargs):
        return [{"trade_date": "20260714", "adj_factor": 2.0}]


def test_tushare_research_provider_maps_structured_facts_and_declares_basis():
    provider = TushareResearchProvider(FakeTushareClient())
    security = provider.fetch_security("600519.SH")
    facts = provider.fetch_financial_facts(
        security_id="sec-1",
        ts_code="600519.SH",
    )
    prices = provider.fetch_market_prices(
        security_id="sec-1",
        ts_code="600519.SH",
        start_date="20260701",
        end_date="20260715",
        basis="forward",
    )
    assert security["exchange"] == "SSE"
    assert {item["statement_type"] for item in facts} == {"income", "balance", "cashflow", "indicator"}
    assert all(item["available_at"].tzinfo is not None for item in facts)
    assert prices[0]["basis"] == "forward"
    assert prices[0]["adj_factor"] == 2.0


class FakeResponse:
    def __init__(self, *, url, payload=None, content=b"", headers=None, history=None):
        self.url = url
        self._payload = payload
        self._content = content
        self.content = content
        self.headers = headers or {}
        self.history = history or []

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=65536):
        del chunk_size
        yield self._content


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.get_calls = 0

    def post(self, url, **_kwargs):
        assert _kwargs["data"]["stock"] == "600519,gssh0600519"
        return FakeResponse(
            url=url,
            payload={
                "announcements": [
                    {
                        "announcementId": "doc-1",
                        "announcementTitle": "2025年年度报告",
                        "announcementTime": 1774692000000,
                        "adjunctUrl": "finalpage/2026-03-28/example.PDF",
                        "secCode": "600519",
                    }
                ]
            },
        )

    def get(self, url, **_kwargs):
        self.get_calls += 1
        if url.endswith("/new/data/szse_stock.json"):
            return FakeResponse(
                url=url,
                payload={
                    "stockList": [
                        {
                            "code": "600519",
                            "orgId": "gssh0600519",
                            "zwjc": "贵州茅台",
                        }
                    ]
                },
                content=b'{"stockList": [{"code": "600519", "orgId": "gssh0600519"}]}',
                headers={"Content-Type": "application/json"},
            )
        return FakeResponse(
            url=url,
            content=b"not-a-pdf",
            headers={"Content-Length": "9", "Content-Type": "application/octet-stream"},
        )


def test_official_disclosure_provider_discovers_and_archives_allowlisted_document(tmp_path):
    provider = OfficialDisclosureProvider(session=FakeSession(), documents_root=tmp_path)
    items = provider.discover_cninfo(symbol="600519", exchange="SSE")
    assert items[0]["document_type"] == "annual_report"
    archived = provider.archive(
        security_id="sec-1",
        external_id=items[0]["external_id"],
        url=items[0]["url"],
    )
    assert Path(archived["storage_path"]).read_bytes() == b"not-a-pdf"
    assert Path(archived["storage_path"]).stat().st_mode & 0o777 == 0o600


def test_official_disclosure_provider_rejects_non_allowlisted_and_non_https_urls(tmp_path):
    provider = OfficialDisclosureProvider(session=FakeSession(), documents_root=tmp_path)
    with pytest.raises(OfficialDisclosureError, match="HTTPS"):
        provider.archive(security_id="sec-1", external_id="bad", url="http://static.cninfo.com.cn/a.pdf")
    with pytest.raises(OfficialDisclosureError, match="allowlisted"):
        provider.archive(security_id="sec-1", external_id="bad", url="https://example.com/a.pdf")


def test_disclosure_ingestion_does_not_redownload_an_archived_source_document(tmp_path):
    database = ResearchDatabase(db_url="sqlite:///:memory:")
    repository = ResearchRepository(database)
    repository.upsert_security(
        {
            "ts_code": "600519.SH",
            "symbol": "600519",
            "exchange": "SSE",
            "name": "贵州茅台",
        }
    )
    session = FakeSession()
    ingestion = ResearchIngestionService(
        repository,
        disclosure_provider=OfficialDisclosureProvider(
            session=session,
            documents_root=tmp_path / "documents",
        ),
    )
    first = ingestion.ingest_cninfo_disclosures(security_identifier="600519")
    second = ingestion.ingest_cninfo_disclosures(security_identifier="600519")
    assert first["created"] == 1
    assert first["archived"] == 1
    assert second["created"] == 0
    assert second["archived"] == 0
    assert session.get_calls == 2
    database.close()
