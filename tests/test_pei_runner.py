from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Mapping, Sequence

import pytest

from src.integrations.codex.fixture_mcp_server import fixture_evidence_ids
from src.integrations.codex.pei_runner import (
    PeiRunRequest,
    PeiRunner,
    PeiRunnerConfig,
    PeiRunnerError,
    ProcessExecutionResult,
)


REPORT_FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "integrations"
    / "codex"
    / "fixtures"
    / "earnings_deep_dive_600519_report.json"
)


def _report_fixture() -> dict:
    return json.loads(REPORT_FIXTURE_PATH.read_text(encoding="utf-8"))


def _config(tmp_path: Path, **overrides) -> PeiRunnerConfig:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text("# test-only dedicated config\n", encoding="utf-8")
    plugin_root = (
        codex_home
        / "plugins"
        / "cache"
        / "test-marketplace"
        / "test-plugin"
        / "fixture-v1"
    )
    (plugin_root / "skills" / "public-equity-investing").mkdir(parents=True)
    (plugin_root / "skills" / "public-equity-investing" / "SKILL.md").write_text(
        "---\nname: public-equity-investing\n---\n",
        encoding="utf-8",
    )
    (plugin_root / ".codex-plugin").mkdir()
    (plugin_root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": "test-plugin", "version": "fixture-v1", "skills": "./skills/"}),
        encoding="utf-8",
    )
    values = {
        "artifact_root": tmp_path / "artifacts",
        "codex_home": codex_home,
        "plugin_skill": "public-equity-investing",
        "plugin_version": "fixture-v1",
        "workflow_version": "earnings-v1",
        "enabled": True,
        "codex_binary": sys.executable,
        "model": None,
        "profile": None,
    }
    values.update(overrides)
    return PeiRunnerConfig(**values)


def _request(run_id: str = "run-001") -> PeiRunRequest:
    return PeiRunRequest(
        run_id=run_id,
        job_id="job-001",
        trace_id="trace-001",
        pack_id="ep_fixture_600519_2025_annual_v1",
        pack_manifest_hash=(
            "sha256:e0e74f1f6ea6c210d55a69c010054da90430bdc43f2f23bdcee896b7013b7dd5"
        ),
        workflow="earnings_deep_dive",
        as_of="2026-07-15T18:00:00+08:00",
        allowed_evidence_ids=frozenset(fixture_evidence_ids()),
    )


def _executor_for(
    report: dict | None,
    *,
    returncode: int = 0,
    stderr: str = "",
    timed_out: bool = False,
    captured: dict | None = None,
    extra_events: Sequence[dict] = (),
):
    def execute(
        command: Sequence[str],
        environment: Mapping[str, str],
        cwd: Path,
        timeout_seconds: int,
        terminate_grace_seconds: float,
        max_capture_bytes: int,
    ) -> ProcessExecutionResult:
        if captured is not None:
            captured.update(
                {
                    "command": list(command),
                    "environment": dict(environment),
                    "cwd": cwd,
                    "timeout_seconds": timeout_seconds,
                    "terminate_grace_seconds": terminate_grace_seconds,
                    "max_capture_bytes": max_capture_bytes,
                }
            )
        if report is not None:
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                json.dumps(report, ensure_ascii=False),
                encoding="utf-8",
            )
        event_payloads = [
            {"type": "thread.started", "thread_id": "thread-fixture"},
            *extra_events,
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 120,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                },
            },
        ]
        events = "\n".join(json.dumps(event) for event in event_payloads)
        return ProcessExecutionResult(
            returncode=returncode,
            stdout=events,
            stderr=stderr,
            duration_seconds=1.25,
            timed_out=timed_out,
        )

    return execute


def test_runner_uses_controlled_command_environment_and_validates_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-leak")
    captured: dict = {}
    config = _config(tmp_path, model="operator/model", profile="pei-worker")
    result = PeiRunner(
        config,
        process_executor=_executor_for(
            _report_fixture(),
            stderr="Authorization: Bearer secret-token",
            captured=captured,
            extra_events=(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "mcp-1",
                        "type": "mcp_tool_call",
                        "server": "dsa_research_fixture",
                        "tool": "get_evidence_pack_manifest",
                        "status": "completed",
                    },
                },
                {
                    "type": "item.completed",
                    "item": {
                        "id": "transport-1",
                        "type": "error",
                        "message": "Falling back from WebSockets to HTTPS transport.",
                    },
                },
            ),
        ),
    ).run(_request())

    command = captured["command"]
    assert Path(command[0]).resolve() == Path(sys.executable).resolve()
    assert command[1] == "exec"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--json" in command
    assert "--strict-config" in command
    assert "--ignore-rules" in command
    assert "--skip-git-repo-check" in command
    assert command[command.index("--model") + 1] == "operator/model"
    assert command[command.index("--profile") + 1] == "pei-worker"
    assert "danger-full-access" not in command
    assert "workspace-write" not in command
    assert "ep_fixture_600519_2025_annual_v1" in command[-1]
    assert "$public-equity-investing" in command[-1]
    model_schema_path = Path(command[command.index("--output-schema") + 1])
    assert model_schema_path.name == "model-output-schema.json"
    model_schema = json.loads(model_schema_path.read_text(encoding="utf-8"))
    assert "uniqueItems" not in json.dumps(model_schema)

    environment = captured["environment"]
    assert environment["CODEX_HOME"] == str(config.codex_home.resolve())
    assert environment["HOME"] == str(config.codex_home.resolve())
    assert "UNRELATED_SECRET" not in environment
    assert result.thread_id == "thread-fixture"
    assert result.usage["input_tokens"] == 120
    assert result.report["schema_version"] == "1.0"

    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["status"] == "validated"
    assert metadata["model"] == "operator/model"
    assert metadata["pack_manifest_hash"] == _request().pack_manifest_hash
    assert metadata["output_schema_version"] == "1.0"
    assert metadata["output_schema_sha256"].startswith("sha256:")
    assert metadata["model_output_schema_sha256"].startswith("sha256:")
    assert metadata["sandbox"] == "read-only"
    assert metadata["mcp_tool_calls"] == [
        {
            "count": 1,
            "server": "dsa_research_fixture",
            "status": "completed",
            "tool": "get_evidence_pack_manifest",
        }
    ]
    assert metadata["tool_item_counts"] == {}
    assert metadata["transport_fallback"] is True
    assert "https_transport_fallback" in metadata["event_warnings"]
    assert "command" not in metadata
    assert "prompt" not in metadata
    assert {item["path"] for item in metadata["artifacts"]} == {
        "events.jsonl",
        "model-output-schema.json",
        "raw-output.json",
        "report.md",
        "stderr.log",
        "validated-report.json",
    }
    assert "secret-token" not in (result.artifact_dir / "stderr.log").read_text(encoding="utf-8")
    assert (result.artifact_dir / "validated-report.json").is_file()
    assert (result.artifact_dir / "report.md").is_file()
    assert list((result.artifact_dir / "workspace").iterdir()) == []
    if os.name != "nt":
        assert (result.artifact_dir / "raw-output.json").stat().st_mode & 0o777 == 0o600


def test_runner_preserves_invalid_output_without_publishing_report(tmp_path: Path) -> None:
    invalid_report = copy.deepcopy(_report_fixture())
    invalid_report["risks"][0]["evidence_ids"] = ["document:not-in-pack"]
    invalid_report["citations"].append(
        {
            "evidence_id": "document:not-in-pack",
            "claim": "Unknown fixture",
            "source_type": "source_document",
        }
    )
    runner = PeiRunner(
        _config(tmp_path),
        process_executor=_executor_for(invalid_report),
    )

    with pytest.raises(PeiRunnerError) as exc_info:
        runner.run(_request())

    error = exc_info.value
    assert error.category == "schema_validation"
    assert error.retryable is False
    assert error.artifact_dir is not None
    assert (error.artifact_dir / "raw-output.json").is_file()
    assert not (error.artifact_dir / "validated-report.json").exists()
    assert not (error.artifact_dir / "report.md").exists()
    metadata = json.loads((error.artifact_dir / "run-metadata.json").read_text(encoding="utf-8"))
    assert metadata["status"] == "failed"
    assert metadata["error"]["category"] == "schema_validation"
    assert "unknown_evidence_id" in {item["code"] for item in metadata["validation_issues"]}


@pytest.mark.parametrize(
    ("execution", "category", "retryable"),
    [
        ({"returncode": 9, "stderr": "token=top-secret"}, "codex_exit", True),
        ({"returncode": -15, "timed_out": True}, "codex_timeout", True),
    ],
)
def test_runner_classifies_process_failures_and_redacts_diagnostics(
    tmp_path: Path,
    execution: dict,
    category: str,
    retryable: bool,
) -> None:
    runner = PeiRunner(
        _config(tmp_path),
        process_executor=_executor_for(None, **execution),
    )

    with pytest.raises(PeiRunnerError) as exc_info:
        runner.run(_request())

    error = exc_info.value
    assert error.category == category
    assert error.retryable is retryable
    assert error.artifact_dir is not None
    metadata_text = (error.artifact_dir / "run-metadata.json").read_text(encoding="utf-8")
    stderr_text = (error.artifact_dir / "stderr.log").read_text(encoding="utf-8")
    assert "top-secret" not in metadata_text
    assert "top-secret" not in stderr_text


def test_runner_is_opt_in_and_does_not_create_artifacts_when_disabled(tmp_path: Path) -> None:
    config = _config(tmp_path, enabled=False)
    runner = PeiRunner(config, process_executor=_executor_for(_report_fixture()))

    with pytest.raises(ValueError, match="RESEARCH_ENABLED is false"):
        runner.run(_request())

    assert not config.artifact_root.exists()


def test_runner_records_process_launch_failure(tmp_path: Path) -> None:
    def fail_to_start(*_args, **_kwargs):
        raise PermissionError("token=launch-secret")

    runner = PeiRunner(_config(tmp_path), process_executor=fail_to_start)

    with pytest.raises(PeiRunnerError) as exc_info:
        runner.run(_request())

    error = exc_info.value
    assert error.category == "codex_launch"
    assert error.retryable is False
    assert error.artifact_dir is not None
    metadata_text = (error.artifact_dir / "run-metadata.json").read_text(encoding="utf-8")
    stderr_text = (error.artifact_dir / "stderr.log").read_text(encoding="utf-8")
    assert "launch-secret" not in metadata_text
    assert "launch-secret" not in stderr_text
    assert (error.artifact_dir / "events.jsonl").is_file()


def test_relative_artifact_root_is_resolved_from_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("RESEARCH_CODEX_HOME", str(codex_home))
    monkeypatch.setenv("RESEARCH_PEI_PLUGIN_SKILL", "public-equity-investing")
    monkeypatch.setenv("RESEARCH_PEI_PLUGIN_VERSION", "fixture-v1")
    monkeypatch.setenv("RESEARCH_PEI_WORKFLOW_VERSION", "earnings-v1")
    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", "data/custom-research")

    config = PeiRunnerConfig.from_env(project_root=tmp_path)

    assert config.artifact_root == tmp_path / "data" / "custom-research"


def test_runner_rejects_default_personal_codex_home(tmp_path: Path) -> None:
    config = _config(tmp_path, codex_home=Path.home() / ".codex")

    with pytest.raises(ValueError, match="must not reuse"):
        config.validate()


def test_runner_rejects_missing_materialized_plugin_skill(tmp_path: Path) -> None:
    config = _config(tmp_path, plugin_skill="missing-skill")

    with pytest.raises(ValueError, match="materialized PEI plugin skill is not found"):
        config.validate()


@pytest.mark.parametrize(
    "unexpected_event",
    [
        {
            "type": "item.completed",
            "item": {"id": "web-1", "type": "web_search"},
        },
        {
            "type": "item.completed",
            "item": {
                "id": "app-1",
                "type": "mcp_tool_call",
                "server": "codex_apps",
                "tool": "slack_search",
                "status": "completed",
            },
        },
    ],
)
def test_runner_rejects_tools_outside_phase0_allowlist(
    tmp_path: Path,
    unexpected_event: dict,
) -> None:
    runner = PeiRunner(
        _config(tmp_path),
        process_executor=_executor_for(
            _report_fixture(),
            extra_events=(unexpected_event,),
        ),
    )

    with pytest.raises(PeiRunnerError) as exc_info:
        runner.run(_request())

    assert exc_info.value.category == "tool_boundary"
    assert exc_info.value.retryable is False
    metadata = json.loads(
        (exc_info.value.artifact_dir / "run-metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["tool_boundary_violations"]
    assert not (exc_info.value.artifact_dir / "validated-report.json").exists()


def test_run_id_cannot_escape_artifact_root(tmp_path: Path) -> None:
    runner = PeiRunner(
        _config(tmp_path),
        process_executor=_executor_for(_report_fixture()),
    )
    request = _request(run_id="../escape")

    with pytest.raises(ValueError, match="unsupported characters"):
        runner.run(request)
