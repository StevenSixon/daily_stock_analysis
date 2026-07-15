# -*- coding: utf-8 -*-
"""Single-consumer external Worker for persistent PEI research jobs."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import quote

import requests

from src.integrations.codex.pei_runner import (
    PeiRunRequest,
    PeiRunner,
    PeiRunnerConfig,
    PeiRunnerError,
)
from src.integrations.codex.research_mcp_server import _validate_base_url


logger = logging.getLogger(__name__)


class ResearchWorkerApiError(RuntimeError):
    pass


class ResearchWorkerApi:
    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: float = 30,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.base_url = _validate_base_url(
            (
                base_url
                or os.getenv("RESEARCH_WORKER_API_URL")
                or "http://127.0.0.1:8000/api/v1/research/worker"
            ).strip()
        )
        self.token = (token or os.getenv("RESEARCH_WORKER_TOKEN") or "").strip()
        if len(self.token) < 32:
            raise ResearchWorkerApiError("RESEARCH_WORKER_TOKEN must contain at least 32 characters")
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self.session = session or requests.Session()
        self.session.trust_env = False

    def post(self, path: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        safe_path = "/".join(quote(segment, safe="") for segment in path.strip("/").split("/"))
        try:
            response = self.session.post(
                f"{self.base_url}/{safe_path}",
                headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                json=dict(payload),
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ResearchWorkerApiError(
                f"Research Worker API request failed: {type(exc).__name__}"
            ) from exc
        try:
            if response.is_redirect:
                raise ResearchWorkerApiError("Research Worker API redirects are not allowed")
            if response.status_code >= 400:
                raise ResearchWorkerApiError(f"Research Worker API returned HTTP {response.status_code}")
            data = response.json()
            if not isinstance(data, dict):
                raise ResearchWorkerApiError("Research Worker API returned a non-object response")
            return data
        except ValueError as exc:
            raise ResearchWorkerApiError("Research Worker API returned invalid JSON") from exc
        finally:
            response.close()


class _Heartbeat:
    def __init__(
        self,
        *,
        api: ResearchWorkerApi,
        job_id: str,
        lease_token: str,
        lease_seconds: int,
        interval_seconds: int,
    ) -> None:
        self.api = api
        self.job_id = job_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        self.interval_seconds = interval_seconds
        self.cancel_requested = threading.Event()
        self.failed = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"research-heartbeat-{job_id}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(1, self.interval_seconds + 1))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                result = self.api.post(
                    f"jobs/{self.job_id}/heartbeat",
                    {"lease_token": self.lease_token, "lease_seconds": self.lease_seconds},
                )
                if result.get("cancel_requested"):
                    self.cancel_requested.set()
            except ResearchWorkerApiError as exc:
                logger.warning("Research job heartbeat failed: %s", exc)
                self.failed.set()
                return


class ResearchWorker:
    def __init__(
        self,
        *,
        project_root: Path,
        api: Optional[ResearchWorkerApi] = None,
        runner_config: Optional[PeiRunnerConfig] = None,
        worker_id: Optional[str] = None,
        lease_seconds: Optional[int] = None,
        heartbeat_seconds: Optional[int] = None,
        poll_seconds: Optional[float] = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.api = api or ResearchWorkerApi()
        self.runner_config = runner_config or PeiRunnerConfig.from_env(project_root=self.project_root)
        self.worker_id = worker_id or os.getenv("RESEARCH_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        self.lease_seconds = max(
            60,
            min(3600, int(lease_seconds or os.getenv("RESEARCH_WORKER_LEASE_SECONDS", "300"))),
        )
        default_heartbeat = max(15, self.lease_seconds // 3)
        self.heartbeat_seconds = max(
            10,
            min(self.lease_seconds - 5, int(heartbeat_seconds or default_heartbeat)),
        )
        self.poll_seconds = max(
            0.5,
            min(60.0, float(poll_seconds or os.getenv("RESEARCH_WORKER_POLL_SECONDS", "5"))),
        )

    def run_once(self) -> bool:
        response = self.api.post(
            "claim",
            {"worker_id": self.worker_id, "lease_seconds": self.lease_seconds},
        )
        if not response.get("claimed"):
            return False
        assignment = response.get("assignment")
        if not isinstance(assignment, Mapping):
            raise ResearchWorkerApiError("claimed Worker response is missing assignment")
        self._execute_assignment(assignment)
        return True

    def run_forever(self, stop_event: Optional[threading.Event] = None) -> None:
        stop = stop_event or threading.Event()
        while not stop.is_set():
            try:
                worked = self.run_once()
            except ResearchWorkerApiError as exc:
                logger.warning("Research Worker polling failed: %s", exc)
                worked = False
            if not worked:
                stop.wait(self.poll_seconds)

    def _execute_assignment(self, assignment: Mapping[str, Any]) -> None:
        job = assignment.get("job")
        pack = assignment.get("pack")
        if not isinstance(job, Mapping) or not isinstance(pack, Mapping):
            raise ResearchWorkerApiError("Worker assignment is missing job or pack")
        job_id = str(job["id"])
        run_id = str(assignment["run_id"])
        lease_token = str(assignment["lease_token"])
        heartbeat = _Heartbeat(
            api=self.api,
            job_id=job_id,
            lease_token=lease_token,
            lease_seconds=self.lease_seconds,
            interval_seconds=self.heartbeat_seconds,
        )
        heartbeat.start()
        runner = PeiRunner(
            replace(
                self.runner_config,
                workflow_version=str(job["workflow_version"]),
            )
        )
        try:
            result = runner.run(
                PeiRunRequest(
                    run_id=run_id,
                    job_id=job_id,
                    trace_id=str(job["trace_id"]),
                    pack_id=str(pack["pack_id"]),
                    pack_manifest_hash=str(pack["manifest_hash"]),
                    workflow=str(job["workflow"]),
                    as_of=str(pack["as_of"]),
                    allowed_evidence_ids=frozenset(str(item) for item in pack["evidence_ids"]),
                )
            )
            metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
            final_heartbeat = self.api.post(
                f"jobs/{job_id}/heartbeat",
                {"lease_token": lease_token, "lease_seconds": self.lease_seconds},
            )
            if heartbeat.cancel_requested.is_set() or final_heartbeat.get("cancel_requested"):
                self._fail(
                    job_id,
                    run_id,
                    lease_token,
                    error_code="cancelled",
                    error_message="Cancellation requested while the Codex process was running",
                    retryable=False,
                    metadata=metadata,
                )
                return
            if heartbeat.failed.is_set():
                raise ResearchWorkerApiError("Research job lease heartbeat stopped before completion")
            self.api.post(
                f"jobs/{job_id}/complete",
                {
                    "run_id": run_id,
                    "lease_token": lease_token,
                    "report": result.report,
                    "run_metadata": metadata,
                    "artifact_path": str(result.artifact_dir),
                },
            )
        except PeiRunnerError as exc:
            metadata = _load_failure_metadata(exc.artifact_dir)
            self._fail(
                job_id,
                run_id,
                lease_token,
                error_code=exc.category,
                error_message=str(exc),
                retryable=exc.retryable,
                metadata=metadata,
            )
        finally:
            heartbeat.stop()

    def _fail(
        self,
        job_id: str,
        run_id: str,
        lease_token: str,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        metadata: Mapping[str, Any],
    ) -> None:
        self.api.post(
            f"jobs/{job_id}/fail",
            {
                "run_id": run_id,
                "lease_token": lease_token,
                "error_code": error_code,
                "error_message": error_message[:2000],
                "retryable": retryable,
                "run_metadata": dict(metadata),
            },
        )


def _load_failure_metadata(artifact_dir: Optional[Path]) -> dict[str, Any]:
    if artifact_dir is None:
        return {}
    path = artifact_dir / "run-metadata.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"artifact_path": str(artifact_dir)}
    if isinstance(value, dict):
        value.setdefault("artifact_path", str(artifact_dir))
        return value
    return {"artifact_path": str(artifact_dir)}
