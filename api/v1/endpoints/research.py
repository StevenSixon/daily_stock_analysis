# -*- coding: utf-8 -*-
"""Versioned Research Center and external Worker API endpoints."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, Query

from api.v1.errors import api_error
from api.v1.schemas.research import (
    ResearchJobCreateRequest,
    ResearchJobResponse,
    ResearchListResponse,
    ResearchRefreshRequest,
    ResearchReviewRequest,
    ResearchStatusResponse,
    ResearchTimelineResponse,
    WorkerClaimRequest,
    WorkerClaimResponse,
    WorkerCompleteRequest,
    WorkerFailRequest,
    WorkerHeartbeatRequest,
)
from src.research.worker_auth import WorkerAuthorizationError, authorize_worker
from src.services.research_service import (
    SUPPORTED_RESEARCH_WORKFLOWS,
    ResearchConflictError,
    ResearchDataBlockedError,
    ResearchNotFoundError,
    ResearchOutputValidationError,
    ResearchService,
    ResearchServiceError,
    get_research_service,
    research_enabled,
)


logger = logging.getLogger(__name__)
router = APIRouter()


def _service() -> ResearchService:
    try:
        return get_research_service()
    except ResearchServiceError as exc:
        raise api_error(503, exc.error_code, str(exc)) from exc


def _worker_scope(scope: str):
    def dependency(authorization: str = Header("")):
        try:
            return authorize_worker(authorization, {scope})
        except WorkerAuthorizationError as exc:
            raise api_error(401, "worker_unauthorized", str(exc)) from exc

    return dependency


def _translate_error(exc: Exception):
    if isinstance(exc, ResearchNotFoundError):
        return api_error(404, exc.error_code, str(exc))
    if isinstance(exc, ResearchConflictError):
        return api_error(409, exc.error_code, str(exc))
    if isinstance(exc, ResearchDataBlockedError):
        return api_error(422, exc.error_code, str(exc))
    if isinstance(exc, ResearchOutputValidationError):
        return api_error(422, exc.error_code, str(exc), detail=exc.issues)
    if isinstance(exc, (ValueError, PermissionError, RuntimeError)):
        return api_error(409, "research_invalid_state", str(exc))
    logger.error("Research API request failed: %s", exc, exc_info=True)
    return api_error(500, "research_internal_error", "Research operation failed")


@router.get("/status", response_model=ResearchStatusResponse)
def research_status() -> ResearchStatusResponse:
    if not research_enabled():
        return ResearchStatusResponse(
            enabled=False,
            tushare_configured=bool((os.getenv("TUSHARE_TOKEN") or "").strip()),
            worker_token_configured=len((os.getenv("RESEARCH_WORKER_TOKEN") or "").strip()) >= 32,
            plugin_skill=(os.getenv("RESEARCH_PEI_PLUGIN_SKILL") or "").strip() or None,
            plugin_version=(os.getenv("RESEARCH_PEI_PLUGIN_VERSION") or "").strip() or None,
            supported_workflows=sorted(SUPPORTED_RESEARCH_WORKFLOWS),
        )
    return ResearchStatusResponse(**_service().status())


@router.post("/jobs", status_code=202, response_model=ResearchJobResponse)
def create_research_job(request: ResearchJobCreateRequest) -> ResearchJobResponse:
    try:
        job, created = _service().create_job(**request.model_dump())
        return ResearchJobResponse(job=job, created=created)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/jobs", response_model=ResearchListResponse)
def list_research_jobs(
    status: Optional[str] = Query(None, max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ResearchListResponse:
    try:
        return ResearchListResponse(**_service().list_jobs(status=status, page=page, page_size=page_size))
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/jobs/{job_id}")
def get_research_job(job_id: str) -> Dict[str, Any]:
    try:
        return _service().get_job(job_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/jobs/{job_id}/cancel")
def cancel_research_job(job_id: str) -> Dict[str, Any]:
    try:
        return _service().cancel_job(job_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/securities/{security_code}/refresh")
def refresh_research_security(
    security_code: str,
    request: ResearchRefreshRequest,
) -> Dict[str, Any]:
    try:
        return _service().refresh_security(security_code, **request.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/securities/{security_code}/timeline", response_model=ResearchTimelineResponse)
def research_timeline(
    security_code: str,
    limit: int = Query(100, ge=1, le=500),
) -> ResearchTimelineResponse:
    try:
        return ResearchTimelineResponse(**_service().security_timeline(security_code, limit=limit))
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/documents/{document_id}")
def get_research_document(document_id: str) -> Dict[str, Any]:
    try:
        return _service().get_document(document_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/reports", response_model=ResearchListResponse)
def list_research_reports(
    status: Optional[str] = Query(None, max_length=32),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ResearchListResponse:
    try:
        return ResearchListResponse(**_service().list_reports(status=status, page=page, page_size=page_size))
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/reports/{report_id}")
def get_research_report(report_id: str) -> Dict[str, Any]:
    try:
        return _service().get_report(report_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/reports/{report_id}/evidence")
def get_report_evidence(report_id: str) -> Dict[str, Any]:
    try:
        return {"items": _service().report_evidence(report_id)}
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/reports/{report_id}/review")
def review_research_report(report_id: str, request: ResearchReviewRequest) -> Dict[str, Any]:
    try:
        return _service().review_report(report_id, **request.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


# Worker API: browser auth is deliberately bypassed; each route uses a scoped
# Bearer token even when ADMIN_AUTH_ENABLED is false.
@router.post("/worker/claim", response_model=WorkerClaimResponse)
def claim_research_job(
    request: WorkerClaimRequest,
    _principal=Depends(_worker_scope("research:job:claim")),
) -> WorkerClaimResponse:
    try:
        assignment = _service().claim_job(**request.model_dump())
        return WorkerClaimResponse(claimed=assignment is not None, assignment=assignment)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/worker/jobs/{job_id}/heartbeat")
def heartbeat_research_job(
    job_id: str,
    request: WorkerHeartbeatRequest,
    _principal=Depends(_worker_scope("research:job:update")),
) -> Dict[str, Any]:
    try:
        return _service().heartbeat(job_id, **request.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/worker/jobs/{job_id}/complete")
def complete_research_job(
    job_id: str,
    request: WorkerCompleteRequest,
    _principal=Depends(_worker_scope("research:report:write")),
) -> Dict[str, Any]:
    try:
        return _service().complete_worker_run(job_id, **request.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.post("/worker/jobs/{job_id}/fail")
def fail_research_job(
    job_id: str,
    request: WorkerFailRequest,
    _principal=Depends(_worker_scope("research:job:update")),
) -> Dict[str, Any]:
    try:
        return _service().fail_worker_run(job_id, **request.model_dump())
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/worker/packs/{pack_id}/filings/{evidence_id}")
def get_worker_filing(
    pack_id: str,
    evidence_id: str,
    _principal=Depends(_worker_scope("research:data:read")),
) -> Dict[str, Any]:
    try:
        return _service().pack_filing(pack_id, evidence_id)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/worker/packs/{pack_id}/securities/{security_code}")
def resolve_worker_security(
    pack_id: str,
    security_code: str,
    _principal=Depends(_worker_scope("research:data:read")),
) -> Dict[str, Any]:
    try:
        return _service().resolve_pack_security(pack_id, security_code)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/worker/packs/{pack_id}/{section}")
def get_worker_pack_section(
    pack_id: str,
    section: str,
    _principal=Depends(_worker_scope("research:data:read")),
) -> Any:
    try:
        return _service().pack_section(pack_id, section)
    except Exception as exc:
        raise _translate_error(exc) from exc


@router.get("/worker/packs/{pack_id}")
def get_worker_pack_manifest(
    pack_id: str,
    _principal=Depends(_worker_scope("research:data:read")),
) -> Dict[str, Any]:
    try:
        return _service().pack_section(pack_id, "manifest")
    except Exception as exc:
        raise _translate_error(exc) from exc
