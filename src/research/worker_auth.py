# -*- coding: utf-8 -*-
"""Independent Bearer-token authorization for the external Research Worker."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from typing import Iterable


DEFAULT_WORKER_SCOPES = frozenset(
    {
        "research:data:read",
        "research:job:claim",
        "research:job:update",
        "research:report:write",
    }
)


class WorkerAuthorizationError(PermissionError):
    pass


@dataclass(frozen=True)
class WorkerPrincipal:
    token_id: str
    scopes: frozenset[str]


def authorize_worker(authorization: str, required_scopes: Iterable[str]) -> WorkerPrincipal:
    configured = (os.getenv("RESEARCH_WORKER_TOKEN") or "").strip()
    if len(configured) < 32:
        raise WorkerAuthorizationError("research worker token is not configured")
    scheme, separator, candidate = (authorization or "").partition(" ")
    if not separator or scheme.lower() != "bearer" or not candidate:
        raise WorkerAuthorizationError("Bearer authorization is required")
    if not hmac.compare_digest(candidate.strip(), configured):
        raise WorkerAuthorizationError("invalid research worker token")
    configured_scopes = frozenset(
        value.strip()
        for value in (os.getenv("RESEARCH_WORKER_SCOPES") or ",".join(sorted(DEFAULT_WORKER_SCOPES))).split(",")
        if value.strip()
    )
    missing = set(required_scopes) - configured_scopes
    if missing:
        raise WorkerAuthorizationError(f"research worker token lacks required scope: {sorted(missing)[0]}")
    return WorkerPrincipal(token_id="env-worker-token", scopes=configured_scopes)
