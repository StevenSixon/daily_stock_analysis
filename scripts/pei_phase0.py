#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Preflight or explicitly run the synthetic PEI Phase 0 vertical slice."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import setup_env  # noqa: E402
from src.integrations.codex.fixture_mcp_server import (  # noqa: E402
    FIXTURE_PACK_ID,
    fixture_evidence_ids,
    load_fixture_pack,
)
from src.integrations.codex.output_validator import PeiOutputValidator  # noqa: E402
from src.integrations.codex.pei_runner import (  # noqa: E402
    PeiRunRequest,
    PeiRunner,
    PeiRunnerConfig,
    PeiRunnerError,
)
from src.services.run_diagnostics import sanitize_diagnostic_text  # noqa: E402


REPORT_FIXTURE_PATH = (
    PROJECT_ROOT
    / "src"
    / "integrations"
    / "codex"
    / "fixtures"
    / "earnings_deep_dive_600519_report.json"
)


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _load_runner() -> PeiRunner:
    setup_env(override=False)
    config = PeiRunnerConfig.from_env(project_root=PROJECT_ROOT)
    return PeiRunner(config)


def _validate_bundled_contracts() -> dict:
    pack = load_fixture_pack(FIXTURE_PACK_ID)
    report = json.loads(REPORT_FIXTURE_PATH.read_text(encoding="utf-8"))
    PeiOutputValidator().validate(
        report,
        expected_workflow=pack["workflow"],
        expected_as_of=pack["as_of"],
        allowed_evidence_ids=fixture_evidence_ids(),
    )
    return {
        "pack_id": pack["pack_id"],
        "manifest_hash": pack["manifest_hash"],
        "evidence_count": len(pack["evidence_manifest"]),
        "report_fixture_valid": True,
        "synthetic_fixture": True,
    }


def _preflight() -> int:
    runner = _load_runner()
    payload = runner.preflight()
    payload["fixture"] = _validate_bundled_contracts()
    _print_json(payload)
    return 0


def _run(args: argparse.Namespace) -> int:
    runner = _load_runner()
    pack = load_fixture_pack(FIXTURE_PACK_ID)
    request = PeiRunRequest(
        run_id=args.run_id or f"phase0-{uuid.uuid4().hex[:12]}",
        job_id=args.job_id or "phase0-fixture-job",
        trace_id=args.trace_id or uuid.uuid4().hex,
        pack_id=pack["pack_id"],
        pack_manifest_hash=pack["manifest_hash"],
        workflow=pack["workflow"],
        as_of=pack["as_of"],
        allowed_evidence_ids=frozenset(fixture_evidence_ids()),
    )
    result = runner.run(request)
    _print_json(
        {
            "status": "validated",
            "run_id": result.run_id,
            "artifact_dir": str(result.artifact_dir),
            "metadata_path": str(result.metadata_path),
            "thread_id": result.thread_id,
            "usage": result.usage,
            "duration_seconds": result.duration_seconds,
            "synthetic_fixture": True,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate or explicitly execute the synthetic DSA + PEI Phase 0 integration. "
            "The run command can consume Codex usage."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "preflight",
        help="Validate local config, Codex CLI, output schema, and bundled synthetic fixture.",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="Explicitly start a Codex run against the synthetic fixture.",
    )
    run_parser.add_argument("--run-id", help="Unique artifact directory identifier.")
    run_parser.add_argument("--job-id", help="Phase 0 job identifier recorded in metadata.")
    run_parser.add_argument("--trace-id", help="Trace identifier recorded in metadata.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preflight":
            return _preflight()
        return _run(args)
    except (ValueError, OSError, json.JSONDecodeError, PeiRunnerError) as exc:
        payload = {
            "status": "failed",
            "error": sanitize_diagnostic_text(str(exc), max_length=500) or "PEI Phase 0 failed",
        }
        if isinstance(exc, PeiRunnerError):
            payload.update(
                {
                    "category": exc.category,
                    "retryable": exc.retryable,
                    "artifact_dir": str(exc.artifact_dir) if exc.artifact_dir else None,
                }
            )
        _print_json(payload)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
