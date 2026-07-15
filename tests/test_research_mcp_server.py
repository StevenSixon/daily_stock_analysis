from __future__ import annotations

import pytest

from src.integrations.codex import research_mcp_server as server


class _FakeClient:
    def __init__(self) -> None:
        self.manifest = {
            "pack_id": "ep_test",
            "manifest_hash": "sha256:" + "a" * 64,
            "as_of": "2026-07-15T10:00:00Z",
            "data_cutoff": "2026-07-14T08:00:00Z",
            "quality": {"coverage": {"facts": 1}, "warnings": []},
            "evidence_manifest": [
                {"evidence_id": "ff:one", "evidence_type": "financial_fact", "source": "tushare"}
            ],
        }

    def get(self, *segments: str):
        if segments == ("packs", "ep_test"):
            return self.manifest
        if segments == ("packs", "ep_test", "financials"):
            return {"facts": [{"evidence_id": "ff:one", "value": 1.0}]}
        raise AssertionError(segments)


def test_research_mcp_envelope_is_frozen_and_cited(monkeypatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr(server, "_client", lambda: fake)
    result = server.get_financial_statements("ep_test")
    assert result["freshness"]["mode"] == "frozen_pack"
    assert result["payload"]["facts"][0]["value"] == 1.0
    assert result["citations"][0]["evidence_id"] == "ff:one"


def test_research_mcp_rejects_insecure_remote_api_url() -> None:
    with pytest.raises(server.ResearchMcpError, match="must use HTTPS"):
        server._validate_base_url("http://research.example/api/v1/research/worker")
    assert server._validate_base_url("http://127.0.0.1:8000/api/v1/research/worker").startswith("http://")
    assert server._validate_base_url("https://research.example/api/v1/research/worker").startswith("https://")
