from __future__ import annotations

import copy
import json

import pytest

from src.integrations.codex.output_validator import (
    PeiOutputValidationError,
    PeiOutputValidator,
    build_codex_output_schema,
)


FACT_REVENUE = "fact:600519.SH:revenue:2025-12-31:v1"
DOCUMENT_ANNUAL = "document:cninfo:fixture-annual-2025"
ALLOWED_EVIDENCE_IDS = {FACT_REVENUE, DOCUMENT_ANNUAL}


def _valid_report() -> dict:
    return {
        "schema_version": "1.0",
        "workflow": "earnings_deep_dive",
        "as_of": "2026-07-15T10:00:00Z",
        "security": {
            "ts_code": "600519.SH",
            "code": "600519",
            "exchange": "SSE",
            "name": "贵州茅台",
            "currency": "CNY",
        },
        "executive_summary": "收入保持增长，但仍需关注需求变化。",
        "thesis": [
            {
                "title": "品牌与渠道韧性",
                "statement": "正式年报支持核心业务保持韧性的判断。",
                "stance": "positive",
                "confidence": 0.75,
                "evidence_ids": [DOCUMENT_ANNUAL],
            }
        ],
        "financial_analysis": {
            "summary": "报告期收入同比增长。",
            "metrics": [
                {
                    "metric_code": "revenue",
                    "label": "营业收入",
                    "value": 170899000000,
                    "unit": "CNY",
                    "currency": "CNY",
                    "period_end": "2025-12-31",
                    "evidence_ids": [FACT_REVENUE],
                }
            ],
        },
        "valuation": {
            "summary": "本次 fixture 不生成正式目标价。",
            "method": None,
            "currency": None,
            "range_low": None,
            "range_high": None,
            "assumptions": ["估值输入不足，仅保留定性结论。"],
            "evidence_ids": [FACT_REVENUE],
        },
        "catalysts": [],
        "risks": [
            {
                "title": "需求波动",
                "description": "需求变化可能影响收入增速。",
                "severity": "medium",
                "evidence_ids": [DOCUMENT_ANNUAL],
            }
        ],
        "invalidation_conditions": [
            {
                "statement": "收入趋势发生显著逆转。",
                "observable": "后续正式财报收入同比明显下降。",
                "evidence_ids": [FACT_REVENUE],
            }
        ],
        "data_gaps": [
            {
                "field": "consensus_estimates",
                "reason": "fixture 未包含一致预期。",
                "impact": "不能判断相对市场预期的超预期程度。",
                "blocking": False,
            }
        ],
        "citations": [
            {
                "evidence_id": FACT_REVENUE,
                "claim": "报告期收入数据。",
                "source_type": "financial_fact",
            },
            {
                "evidence_id": DOCUMENT_ANNUAL,
                "claim": "正式年度报告。",
                "source_type": "source_document",
            },
        ],
        "markdown": "# 贵州茅台财报深挖\n\n本报告仅使用冻结的 fixture evidence。",
    }


def test_validator_accepts_equivalent_timezone_and_known_evidence() -> None:
    result = PeiOutputValidator().validate(
        _valid_report(),
        expected_workflow="earnings_deep_dive",
        expected_as_of="2026-07-15T18:00:00+08:00",
        allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
    )

    assert result["security"]["ts_code"] == "600519.SH"


def test_validator_rejects_unknown_evidence_id() -> None:
    report = _valid_report()
    report["risks"][0]["evidence_ids"] = ["document:unknown"]
    report["citations"].append(
        {
            "evidence_id": "document:unknown",
            "claim": "Unknown evidence",
            "source_type": "source_document",
        }
    )

    with pytest.raises(PeiOutputValidationError) as exc_info:
        PeiOutputValidator().validate(
            report,
            expected_workflow="earnings_deep_dive",
            expected_as_of="2026-07-15T18:00:00+08:00",
            allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
        )

    assert {issue.code for issue in exc_info.value.issues} == {"unknown_evidence_id"}


def test_validator_rejects_body_evidence_without_citation_entry() -> None:
    report = _valid_report()
    report["citations"] = [report["citations"][0]]

    with pytest.raises(PeiOutputValidationError) as exc_info:
        PeiOutputValidator().validate(
            report,
            expected_workflow="earnings_deep_dive",
            expected_as_of="2026-07-15T18:00:00+08:00",
            allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
        )

    assert {issue.code for issue in exc_info.value.issues} == {"missing_citation"}


def test_validator_rejects_schema_and_request_mismatches() -> None:
    report = _valid_report()
    report["financial_analysis"]["metrics"][0].pop("evidence_ids")

    with pytest.raises(PeiOutputValidationError) as schema_exc:
        PeiOutputValidator().validate(
            report,
            expected_workflow="earnings_deep_dive",
            expected_as_of="2026-07-15T18:00:00+08:00",
            allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
        )
    assert "schema_validation" in {issue.code for issue in schema_exc.value.issues}

    report = _valid_report()
    with pytest.raises(PeiOutputValidationError) as semantic_exc:
        PeiOutputValidator().validate(
            report,
            expected_workflow="dcf",
            expected_as_of="2026-07-16T18:00:00+08:00",
            allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
        )
    assert {issue.code for issue in semantic_exc.value.issues} == {
        "workflow_mismatch",
        "as_of_mismatch",
    }


def test_validator_rejects_duplicate_json_keys_without_silent_overwrite() -> None:
    raw = json.dumps(_valid_report(), ensure_ascii=False)
    raw_with_duplicate = raw.replace(
        '"schema_version": "1.0"',
        '"schema_version": "1.0", "schema_version": "2.0"',
        1,
    )

    with pytest.raises(PeiOutputValidationError) as exc_info:
        PeiOutputValidator().validate(
            raw_with_duplicate,
            expected_workflow="earnings_deep_dive",
            expected_as_of="2026-07-15T18:00:00+08:00",
            allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
        )

    assert {issue.code for issue in exc_info.value.issues} == {"invalid_json"}


def test_validator_does_not_mutate_mapping_input() -> None:
    report = _valid_report()
    original = copy.deepcopy(report)

    PeiOutputValidator().validate(
        report,
        expected_workflow="earnings_deep_dive",
        expected_as_of="2026-07-15T18:00:00+08:00",
        allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
    )

    assert report == original


def test_codex_transport_schema_uses_supported_subset_but_local_schema_stays_strict() -> None:
    transport_schema = build_codex_output_schema()
    serialized = json.dumps(transport_schema, sort_keys=True)

    for unsupported in (
        "format",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "uniqueItems",
    ):
        assert f'"{unsupported}"' not in serialized
    assert transport_schema["properties"]["schema_version"]["enum"] == ["1.0"]

    report = _valid_report()
    report["thesis"][0]["evidence_ids"].append(DOCUMENT_ANNUAL)
    with pytest.raises(PeiOutputValidationError) as exc_info:
        PeiOutputValidator().validate(
            report,
            expected_workflow="earnings_deep_dive",
            expected_as_of="2026-07-15T18:00:00+08:00",
            allowed_evidence_ids=ALLOWED_EVIDENCE_IDS,
        )
    assert {issue.code for issue in exc_info.value.issues} == {"schema_validation"}
