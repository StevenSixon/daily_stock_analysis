# -*- coding: utf-8 -*-
"""Build immutable, replayable Evidence Packs from point-in-time research data."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from jsonschema import Draft202012Validator

from src.research.quality_gate import ResearchQualityGate
from src.research.repositories import ResearchRepository, json_loads


EVIDENCE_PACK_SCHEMA_PATH = Path(__file__).with_name("schemas") / "evidence-pack-v1.schema.json"


def _iso(value: datetime | date | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    normalized = dict(payload)
    normalized.pop("manifest_hash", None)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def calculate_pack_hash(payload: Mapping[str, Any]) -> str:
    return f"sha256:{hashlib.sha256(_canonical_bytes(payload)).hexdigest()}"


class EvidencePackBuilder:
    """Freeze a point-in-time research input and persist its content hash."""

    def __init__(
        self,
        repository: Optional[ResearchRepository] = None,
        *,
        pack_root: Optional[Path] = None,
        documents_root: Optional[Path] = None,
        quality_gate: Optional[ResearchQualityGate] = None,
    ) -> None:
        self.repository = repository or ResearchRepository()
        self.pack_root = (pack_root or Path(os.getenv("RESEARCH_EVIDENCE_PACKS_DIR", "./data/research/evidence-packs"))).expanduser().resolve()
        self.documents_root = (documents_root or Path(os.getenv("RESEARCH_DOCUMENTS_DIR", "./data/research/documents"))).expanduser().resolve()
        self.quality_gate = quality_gate or ResearchQualityGate()
        self._validator = Draft202012Validator(json.loads(EVIDENCE_PACK_SCHEMA_PATH.read_text(encoding="utf-8")))

    def build(
        self,
        *,
        security_identifier: str,
        workflow: str,
        as_of: datetime,
        price_basis: str = "raw",
    ) -> dict[str, Any]:
        as_of_naive = _utc_naive(as_of)
        security = self.repository.get_security(security_identifier)
        if security is None:
            raise KeyError(f"research security not found: {security_identifier}")
        facts = self.repository.financial_facts_as_of(security.id, as_of_naive)
        documents = self.repository.documents_as_of(security.id, as_of_naive)
        prices = self.repository.market_prices_as_of(
            security.id,
            as_of_naive,
            basis=price_basis,
        )
        actions = self.repository.corporate_actions_as_of(security.id, as_of_naive)
        previous_reports = self.repository.published_reports_as_of(security.id, as_of_naive)

        financial_payload = [self._financial_fact(row) for row in facts]
        filing_payload = [self._document(row) for row in documents]
        price_payload = [self._market_price(row) for row in prices]
        action_payload = [self._corporate_action(row) for row in actions]
        previous_research = [self._previous_report(row) for row in previous_reports]
        quality = self.quality_gate.evaluate(
            workflow=workflow,
            financial_facts=financial_payload,
            filings=filing_payload,
            prices=price_payload,
            corporate_actions=action_payload,
        )
        all_times = [
            *(row.available_at for row in facts),
            *(row.available_at for row in documents),
            *(row.available_at for row in prices),
            *(row.available_at for row in actions),
            *(row.published_at for row in previous_reports if row.published_at is not None),
        ]
        data_cutoff = max(all_times) if all_times else as_of_naive
        manifest = self._manifest(facts, documents, prices, actions, previous_reports)
        profile = json_loads(security.profile_json, {})
        base_payload: dict[str, Any] = {
            "schema_version": "1.0",
            "security": {
                "security_id": security.id,
                "ts_code": security.ts_code,
                "symbol": security.symbol,
                "exchange": security.exchange,
                "market": security.market,
                "name": security.name,
                "industry": security.industry,
                "currency": security.currency,
            },
            "as_of": _iso(as_of_naive),
            "data_cutoff": _iso(data_cutoff),
            "workflow": workflow,
            "company_profile": profile,
            "financials": {"facts": financial_payload},
            "market_data": {"price_basis": price_basis, "prices": price_payload},
            "corporate_actions": action_payload,
            "filings": filing_payload,
            "news_events": [],
            "previous_research": previous_research,
            "evidence_manifest": manifest,
            "quality": quality.to_dict(),
        }
        identity_hash = hashlib.sha256(_canonical_bytes(base_payload)).hexdigest()
        base_payload["pack_id"] = f"ep_{identity_hash[:32]}"
        base_payload["manifest_hash"] = calculate_pack_hash(base_payload)
        self._validator.validate(base_payload)
        path = self._write_pack(base_payload)
        self.repository.save_evidence_pack(
            {
                "pack_id": base_payload["pack_id"],
                "security_id": security.id,
                "workflow": workflow,
                "as_of": as_of_naive,
                "data_cutoff": data_cutoff,
                "schema_version": "1.0",
                "manifest_path": str(path),
                "manifest_hash": base_payload["manifest_hash"],
                "quality_status": quality.status,
                "coverage": quality.coverage,
                "warnings": quality.warnings,
                "blocking_gaps": quality.blocking_gaps,
            }
        )
        return base_payload

    def load(self, pack_id: str) -> dict[str, Any]:
        record = self.repository.get_evidence_pack(pack_id)
        if record is None:
            raise KeyError(pack_id)
        path = Path(record.manifest_path).resolve()
        if not path.is_relative_to(self.pack_root):
            raise ValueError("evidence pack path escaped configured pack root")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self._validator.validate(payload)
        if payload.get("pack_id") != pack_id or calculate_pack_hash(payload) != record.manifest_hash:
            raise ValueError("evidence pack identity or hash mismatch")
        return payload

    def _write_pack(self, payload: Mapping[str, Any]) -> Path:
        self.pack_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = (self.pack_root / f"{payload['pack_id']}.json").resolve()
        if not target.is_relative_to(self.pack_root):
            raise ValueError("evidence pack path escaped configured root")
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if target.exists():
            current = json.loads(target.read_text(encoding="utf-8"))
            if calculate_pack_hash(current) != payload["manifest_hash"]:
                raise ValueError("existing evidence pack differs from immutable payload")
            return target
        descriptor, temporary = tempfile.mkstemp(prefix=".evidence-pack-", dir=str(self.pack_root))
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if os.path.exists(temporary):
                os.unlink(temporary)
        return target

    def _document(self, row) -> dict[str, Any]:
        return {
            "evidence_id": f"doc:{row.id}",
            "document_id": row.id,
            "source": row.source_name,
            "external_id": row.external_id,
            "document_type": row.document_type,
            "title": row.title,
            "published_at": _iso(row.published_at),
            "available_at": _iso(row.available_at),
            "period_end": _iso(row.period_end),
            "url": row.url,
            "sha256": row.sha256,
            "content_excerpt": self._read_excerpt(row.parsed_text_path),
            "content_trust": "untrusted_data",
        }

    def _read_excerpt(self, raw_path: Optional[str]) -> Optional[str]:
        if not raw_path:
            return None
        path = Path(raw_path).expanduser().resolve()
        if not path.is_relative_to(self.documents_root) or not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:12000]
        except OSError:
            return None

    @staticmethod
    def _financial_fact(row) -> dict[str, Any]:
        return {
            "evidence_id": f"ff:{row.id}",
            "metric_code": row.metric_code,
            "statement_type": row.statement_type,
            "period_end": _iso(row.period_end),
            "announced_at": _iso(row.announced_at),
            "available_at": _iso(row.available_at),
            "value": row.value,
            "unit": row.unit,
            "currency": row.currency,
            "scope": row.scope,
            "report_type": row.report_type,
            "revision_no": row.revision_no,
            "transform_version": row.transform_version,
            "quality": row.quality,
            "source": {
                "type": row.source_name,
                "record_id": row.source_record_id,
                "document_id": row.document_id,
            },
        }

    @staticmethod
    def _market_price(row) -> dict[str, Any]:
        return {
            "evidence_id": f"px:{row.id}",
            "trade_date": _iso(row.trade_date),
            "basis": row.basis,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
            "adj_factor": row.adj_factor,
            "currency": row.currency,
            "source": row.source_name,
            "available_at": _iso(row.available_at),
        }

    @staticmethod
    def _corporate_action(row) -> dict[str, Any]:
        return {
            "evidence_id": f"ca:{row.id}",
            "action_type": row.action_type,
            "announced_at": _iso(row.announced_at),
            "available_at": _iso(row.available_at),
            "record_date": _iso(row.record_date),
            "ex_date": _iso(row.ex_date),
            "effective_date": _iso(row.effective_date),
            "amount_per_share": row.amount_per_share,
            "ratio": row.ratio,
            "currency": row.currency,
            "source": row.source_name,
        }

    @staticmethod
    def _previous_report(row) -> dict[str, Any]:
        structured = json_loads(row.structured_json, {})
        return {
            "evidence_id": f"report:{row.id}",
            "report_id": row.id,
            "report_type": row.report_type,
            "as_of": _iso(row.as_of),
            "published_at": _iso(row.published_at),
            "executive_summary": structured.get("executive_summary"),
            "content_sha256": row.content_sha256,
            "workflow_version": row.workflow_version,
        }

    @staticmethod
    def _manifest(facts, documents, prices, actions, previous_reports) -> list[dict[str, Any]]:
        items = [
            {
                "evidence_id": f"ff:{row.id}",
                "evidence_type": "financial_fact",
                "source": row.source_name,
            }
            for row in facts
        ]
        items.extend(
            {
                "evidence_id": f"doc:{row.id}",
                "evidence_type": "source_document",
                "source": row.source_name,
            }
            for row in documents
        )
        items.extend(
            {
                "evidence_id": f"px:{row.id}",
                "evidence_type": "market_price",
                "source": row.source_name,
            }
            for row in prices
        )
        items.extend(
            {
                "evidence_id": f"ca:{row.id}",
                "evidence_type": "corporate_action",
                "source": row.source_name,
            }
            for row in actions
        )
        items.extend(
            {
                "evidence_id": f"report:{row.id}",
                "evidence_type": "research_report",
                "source": "dsa_research",
            }
            for row in previous_reports
        )
        return sorted(items, key=lambda item: item["evidence_id"])
