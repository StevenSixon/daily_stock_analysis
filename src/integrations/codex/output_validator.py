# -*- coding: utf-8 -*-
"""Validate PEI structured output before it can enter the research domain."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Set, Union

from jsonschema import Draft202012Validator, FormatChecker


DEFAULT_SCHEMA_PATH = Path(__file__).with_name("schemas") / "pei-report-v1.schema.json"

# Codex Structured Outputs accepts a deliberately small JSON Schema subset. Keep
# the richer contract for local validation, but remove constraints that the API
# cannot enforce before passing a schema to ``codex exec --output-schema``.
_CODEX_SCHEMA_KEYS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "anyOf",
        "definitions",
        "description",
        "enum",
        "items",
        "oneOf",
        "properties",
        "required",
        "type",
    }
)


@dataclass(frozen=True)
class ValidationIssue:
    """One deterministic output validation failure."""

    code: str
    message: str
    path: str = "$"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "path": self.path}


class PeiOutputValidationError(ValueError):
    """Raised when PEI output fails schema or evidence-boundary validation."""

    def __init__(self, issues: Sequence[ValidationIssue]):
        normalized = tuple(issues)
        if not normalized:
            normalized = (
                ValidationIssue(
                    code="unknown_validation_error",
                    message="PEI output validation failed",
                ),
            )
        self.issues = normalized
        super().__init__("; ".join(issue.message for issue in normalized))


class _DuplicateJsonKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


@lru_cache(maxsize=8)
def _load_schema(schema_path: str) -> dict[str, Any]:
    with Path(schema_path).open("r", encoding="utf-8") as handle:
        schema = json.load(handle, object_pairs_hook=_reject_duplicate_keys)
    Draft202012Validator.check_schema(schema)
    return schema


def build_codex_output_schema(
    schema_path: Optional[Union[str, Path]] = None,
) -> dict[str, Any]:
    """Return the model-facing Structured Outputs subset of the full contract.

    Constraints such as ``uniqueItems``, string formats, length bounds, and
    numeric ranges remain authoritative in :class:`PeiOutputValidator` after
    generation. A single-value ``const`` is represented as an equivalent enum
    because enum is part of the supported transport subset.
    """
    resolved = Path(schema_path or DEFAULT_SCHEMA_PATH).resolve()
    adapted = _adapt_schema_node(_load_schema(str(resolved)))
    Draft202012Validator.check_schema(adapted)
    return adapted


def _adapt_schema_node(node: Mapping[str, Any]) -> dict[str, Any]:
    adapted: dict[str, Any] = {}
    for key, value in node.items():
        if key == "const":
            if "enum" not in node:
                adapted["enum"] = [copy.deepcopy(value)]
            continue
        if key not in _CODEX_SCHEMA_KEYS:
            continue
        if key in {"properties", "$defs", "definitions"}:
            if isinstance(value, Mapping):
                adapted[key] = {
                    str(name): _adapt_schema_node(child)
                    for name, child in value.items()
                    if isinstance(child, Mapping)
                }
            continue
        if key in {"anyOf", "oneOf", "allOf"}:
            if isinstance(value, list):
                adapted[key] = [
                    _adapt_schema_node(child)
                    for child in value
                    if isinstance(child, Mapping)
                ]
            continue
        if key == "items":
            if isinstance(value, Mapping):
                adapted[key] = _adapt_schema_node(value)
            continue
        if key == "additionalProperties" and isinstance(value, Mapping):
            adapted[key] = _adapt_schema_node(value)
            continue
        adapted[key] = copy.deepcopy(value)
    return adapted


def _json_path(parts: Iterable[Union[str, int]]) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def _parse_rfc3339(value: str, *, field: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit timezone offset")
    return parsed.astimezone(timezone.utc)


def _collect_evidence_ids(value: Any) -> Set[str]:
    evidence_ids: Set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "evidence_id" and isinstance(item, str):
                evidence_ids.add(item)
            elif key == "evidence_ids" and isinstance(item, list):
                evidence_ids.update(entry for entry in item if isinstance(entry, str))
            else:
                evidence_ids.update(_collect_evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            evidence_ids.update(_collect_evidence_ids(item))
    return evidence_ids


class PeiOutputValidator:
    """Apply JSON Schema plus report/evidence semantic validation."""

    def __init__(self, schema_path: Optional[Union[str, Path]] = None):
        self.schema_path = Path(schema_path or DEFAULT_SCHEMA_PATH).resolve()
        schema = _load_schema(str(self.schema_path))
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker())

    def validate(
        self,
        raw_output: Union[str, bytes, Mapping[str, Any]],
        *,
        expected_workflow: str,
        expected_as_of: str,
        allowed_evidence_ids: Iterable[str],
    ) -> dict[str, Any]:
        """Return a validated JSON-compatible report or raise with all known issues."""
        payload = self._decode(raw_output)
        schema_issues = [
            ValidationIssue(
                code="schema_validation",
                message=error.message,
                path=_json_path(error.absolute_path),
            )
            for error in sorted(self._validator.iter_errors(payload), key=lambda item: list(item.absolute_path))
        ]
        if schema_issues:
            raise PeiOutputValidationError(schema_issues[:50])

        issues: list[ValidationIssue] = []
        if payload["workflow"] != expected_workflow:
            issues.append(
                ValidationIssue(
                    code="workflow_mismatch",
                    message=(
                        f"workflow {payload['workflow']!r} does not match "
                        f"expected workflow {expected_workflow!r}"
                    ),
                    path="$.workflow",
                )
            )

        try:
            actual_as_of = _parse_rfc3339(payload["as_of"], field="as_of")
            required_as_of = _parse_rfc3339(expected_as_of, field="expected_as_of")
            if actual_as_of != required_as_of:
                issues.append(
                    ValidationIssue(
                        code="as_of_mismatch",
                        message="report as_of does not match the frozen Evidence Pack as_of",
                        path="$.as_of",
                    )
                )
        except ValueError as exc:
            issues.append(
                ValidationIssue(
                    code="invalid_as_of",
                    message=str(exc),
                    path="$.as_of",
                )
            )

        allowed = {item for item in allowed_evidence_ids if isinstance(item, str) and item}
        referenced = _collect_evidence_ids(payload)
        unknown = sorted(referenced - allowed)
        if unknown:
            issues.append(
                ValidationIssue(
                    code="unknown_evidence_id",
                    message=f"report references Evidence IDs outside the frozen pack: {', '.join(unknown)}",
                    path="$",
                )
            )

        citation_ids = {citation["evidence_id"] for citation in payload["citations"]}
        body = {key: value for key, value in payload.items() if key != "citations"}
        missing_citations = sorted(_collect_evidence_ids(body) - citation_ids)
        if missing_citations:
            issues.append(
                ValidationIssue(
                    code="missing_citation",
                    message=(
                        "Evidence IDs used in report sections require citation entries: "
                        f"{', '.join(missing_citations)}"
                    ),
                    path="$.citations",
                )
            )

        if issues:
            raise PeiOutputValidationError(issues)
        return payload

    @staticmethod
    def _decode(raw_output: Union[str, bytes, Mapping[str, Any]]) -> dict[str, Any]:
        if isinstance(raw_output, Mapping):
            return copy.deepcopy(dict(raw_output))
        if isinstance(raw_output, bytes):
            try:
                raw_output = raw_output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PeiOutputValidationError(
                    [ValidationIssue(code="invalid_encoding", message="PEI output must be UTF-8 JSON")]
                ) from exc
        if not isinstance(raw_output, str):
            raise PeiOutputValidationError(
                [ValidationIssue(code="invalid_type", message="PEI output must be a JSON object or JSON text")]
            )
        try:
            payload = json.loads(raw_output, object_pairs_hook=_reject_duplicate_keys)
        except (_DuplicateJsonKeyError, json.JSONDecodeError) as exc:
            raise PeiOutputValidationError(
                [ValidationIssue(code="invalid_json", message=f"PEI output is not valid JSON: {exc}")]
            ) from exc
        if not isinstance(payload, dict):
            raise PeiOutputValidationError(
                [ValidationIssue(code="invalid_root", message="PEI output JSON root must be an object")]
            )
        return payload
