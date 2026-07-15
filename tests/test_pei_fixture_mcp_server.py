from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.integrations.codex.fixture_mcp_server import (
    FIXTURE_PACK_ID,
    FixtureEvidenceError,
    calculate_manifest_hash,
    fixture_evidence_ids,
    load_fixture_pack,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
EXPECTED_TOOL_NAMES = {
    "resolve_security",
    "get_evidence_pack_manifest",
    "get_financial_statements",
    "get_market_history",
    "get_filing_excerpt",
}
FORBIDDEN_TOOL_NAMES = {
    "execute_sql",
    "run_shell",
    "fetch_any_url",
    "write_any_file",
    "update_database",
}


def test_fixture_pack_is_hash_verified_and_copy_isolated() -> None:
    first = load_fixture_pack()
    assert first["manifest_hash"] == calculate_manifest_hash(first)
    assert fixture_evidence_ids() == {
        "fact:600519.SH:revenue:2025-12-31:v1",
        "fact:600519.SH:net_profit:2025-12-31:v1",
        "price:600519.SH:2026-07-15:raw:v1",
        "document:cninfo:fixture-annual-2025",
    }

    first["security"]["code"] = "tampered"
    assert load_fixture_pack()["security"]["code"] == "600519"


def test_fixture_pack_rejects_unknown_pack_id() -> None:
    with pytest.raises(FixtureEvidenceError, match="unknown fixture pack_id"):
        load_fixture_pack("../../not-allowlisted")


def test_stdio_mcp_only_exposes_read_only_allowlisted_tools() -> None:
    async def exercise_server() -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "src.integrations.codex.fixture_mcp_server"],
            cwd=ROOT_DIR,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools_response = await session.list_tools()
                tools = {tool.name: tool for tool in tools_response.tools}

                assert set(tools) == EXPECTED_TOOL_NAMES
                assert set(tools).isdisjoint(FORBIDDEN_TOOL_NAMES)
                for tool in tools.values():
                    assert tool.annotations is not None
                    assert tool.annotations.readOnlyHint is True
                    assert tool.annotations.destructiveHint is False
                    assert tool.annotations.idempotentHint is True
                    assert tool.annotations.openWorldHint is False

                manifest_result = await session.call_tool(
                    "get_evidence_pack_manifest",
                    {"pack_id": FIXTURE_PACK_ID},
                )
                assert manifest_result.isError is False
                envelope = manifest_result.structuredContent
                assert envelope is not None
                assert envelope["payload"]["pack_id"] == FIXTURE_PACK_ID
                assert envelope["freshness"]["mode"] == "frozen_fixture"
                assert {item["evidence_id"] for item in envelope["citations"]} == fixture_evidence_ids()

                filing_result = await session.call_tool(
                    "get_filing_excerpt",
                    {
                        "pack_id": FIXTURE_PACK_ID,
                        "evidence_id": "document:cninfo:fixture-annual-2025",
                    },
                )
                assert filing_result.isError is False
                filing_envelope = filing_result.structuredContent
                assert filing_envelope is not None
                assert filing_envelope["payload"]["filing"]["content_is_untrusted"] is True
                assert "filing_content_is_untrusted_data" in filing_envelope["warnings"]

    asyncio.run(exercise_server())
