# -*- coding: utf-8 -*-
"""Public and Worker API contracts for the PEI Research Center."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ResearchStatusResponse(BaseModel):
    enabled: bool
    database_ready: bool = False
    tushare_configured: bool = False
    worker_token_configured: bool = False
    plugin_skill: Optional[str] = None
    plugin_version: Optional[str] = None
    supported_workflows: List[str] = Field(default_factory=list)
    run_token_budget: int = 0
    monthly_token_budget: int = 0
    current_month_tokens: int = 0


class ResearchJobCreateRequest(BaseModel):
    security_code: str = Field(..., min_length=6, max_length=32)
    workflow: str = Field("earnings_deep_dive", min_length=1, max_length=64)
    as_of: Optional[datetime] = None
    trigger_reason: str = Field("manual", min_length=1, max_length=64)
    source_event_id: Optional[str] = Field(None, max_length=192)
    priority: int = Field(100, ge=0, le=1000)
    idempotency_key: Optional[str] = Field(None, min_length=8, max_length=255)
    price_basis: Literal["raw", "forward", "backward"] = "raw"


class ResearchRefreshRequest(BaseModel):
    years: int = Field(5, ge=1, le=15)
    price_basis: Literal["raw", "forward", "backward"] = "raw"
    include_disclosures: bool = True


class ResearchReviewRequest(BaseModel):
    decision: Literal["approve", "reject", "request_changes"]
    note: Optional[str] = Field(None, max_length=4000)


class ResearchJobResponse(BaseModel):
    job: Dict[str, Any]
    created: bool = False


class ResearchListResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)
    total: int = 0


class ResearchTimelineResponse(BaseModel):
    security: Dict[str, Any]
    items: List[Dict[str, Any]] = Field(default_factory=list)


class WorkerClaimRequest(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    lease_seconds: int = Field(300, ge=30, le=3600)


class WorkerClaimResponse(BaseModel):
    claimed: bool
    assignment: Optional[Dict[str, Any]] = None


class WorkerHeartbeatRequest(BaseModel):
    lease_token: str = Field(..., min_length=16, max_length=128)
    lease_seconds: int = Field(300, ge=30, le=3600)


class WorkerCompleteRequest(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=64)
    lease_token: str = Field(..., min_length=16, max_length=128)
    report: Dict[str, Any]
    run_metadata: Dict[str, Any] = Field(default_factory=dict)
    artifact_path: Optional[str] = Field(None, max_length=1000)


class WorkerFailRequest(BaseModel):
    run_id: str = Field(..., min_length=1, max_length=64)
    lease_token: str = Field(..., min_length=16, max_length=128)
    error_code: str = Field(..., min_length=1, max_length=64)
    error_message: str = Field(..., min_length=1, max_length=2000)
    retryable: bool = True
    run_metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("error_code")
    @classmethod
    def validate_error_code(cls, value: str) -> str:
        normalized = value.strip().lower().replace(" ", "_")
        if not normalized or not all(character.isalnum() or character in "_-" for character in normalized):
            raise ValueError("error_code contains unsupported characters")
        return normalized
