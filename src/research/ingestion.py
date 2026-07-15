# -*- coding: utf-8 -*-
"""Application service that persists provider outputs into the Research Domain."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.research.providers.official_disclosure import OfficialDisclosureProvider
from src.research.providers.tushare_research import TushareResearchProvider
from src.research.repositories import ResearchRepository


class ResearchIngestionService:
    """Ingest source data with explicit per-capability degradation diagnostics."""

    def __init__(
        self,
        repository: ResearchRepository,
        *,
        tushare_provider: Optional[TushareResearchProvider] = None,
        disclosure_provider: Optional[OfficialDisclosureProvider] = None,
    ) -> None:
        self.repository = repository
        self.tushare = tushare_provider
        self.disclosures = disclosure_provider

    def ingest_tushare_security(
        self,
        *,
        ts_code: str,
        start_date: str,
        end_date: str,
        price_basis: str = "raw",
    ) -> dict[str, Any]:
        if self.tushare is None:
            raise ValueError("TushareResearchProvider is not configured")
        security_payload = self.tushare.fetch_security(ts_code)
        security = self.repository.upsert_security(security_payload)
        counts = {"financial_facts": 0, "corporate_actions": 0, "market_prices": 0}
        warnings: list[str] = []

        try:
            facts = self.tushare.fetch_financial_facts(
                security_id=security.id,
                ts_code=security.ts_code,
                start_date=start_date,
                end_date=end_date,
            )
            warnings.extend(self.tushare.warnings)
            for item in facts:
                _, created = self.repository.add_financial_fact(item)
                counts["financial_facts"] += int(created)
        except Exception as exc:
            warnings.append(f"financial_ingestion_failed:{type(exc).__name__}")

        try:
            for item in self.tushare.fetch_corporate_actions(
                security_id=security.id,
                ts_code=security.ts_code,
            ):
                _, created = self.repository.add_corporate_action(item)
                counts["corporate_actions"] += int(created)
        except Exception as exc:
            warnings.append(f"corporate_action_ingestion_failed:{type(exc).__name__}")

        try:
            for item in self.tushare.fetch_market_prices(
                security_id=security.id,
                ts_code=security.ts_code,
                start_date=start_date,
                end_date=end_date,
                basis=price_basis,
            ):
                _, created = self.repository.add_market_price(item)
                counts["market_prices"] += int(created)
        except Exception as exc:
            warnings.append(f"market_price_ingestion_failed:{type(exc).__name__}")

        return {
            "security_id": security.id,
            "ts_code": security.ts_code,
            "counts": counts,
            "warnings": list(dict.fromkeys(warnings)),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

    def ingest_cninfo_disclosures(
        self,
        *,
        security_identifier: str,
        page: int = 1,
        page_size: int = 30,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> dict[str, Any]:
        if self.disclosures is None:
            raise ValueError("OfficialDisclosureProvider is not configured")
        security = self.repository.get_security(security_identifier)
        if security is None:
            raise KeyError(security_identifier)
        discovered = self.disclosures.discover_cninfo(
            symbol=security.symbol,
            exchange=security.exchange,
            page=page,
            page_size=page_size,
            start_date=start_date,
            end_date=end_date,
        )
        created_count = 0
        archived_count = 0
        warnings: list[str] = []
        documents = []
        for item in discovered:
            existing = self.repository.get_document_by_source(
                str(item["source_name"]),
                str(item["external_id"]),
            )
            if existing is not None and existing.storage_path:
                documents.append(
                    {
                        "document_id": existing.id,
                        "external_id": existing.external_id,
                        "created": False,
                        "archived": False,
                        "document_type": existing.document_type,
                        "title": existing.title,
                        "published_at": existing.published_at.isoformat(),
                    }
                )
                continue
            archived: dict[str, Any] = {}
            try:
                archived = self.disclosures.archive(
                    security_id=security.id,
                    external_id=str(item["external_id"]),
                    url=str(item["url"]),
                )
                archived_count += 1
                if archived.get("parse_warning"):
                    warnings.append(f"document_parse_degraded:{item['external_id']}")
            except Exception as exc:
                warnings.append(f"document_archive_failed:{item['external_id']}:{type(exc).__name__}")
            if existing is not None:
                if archived:
                    existing = self.repository.update_document_archive(existing.id, archived)
                documents.append(
                    {
                        "document_id": existing.id,
                        "external_id": existing.external_id,
                        "created": False,
                        "archived": bool(archived),
                        "document_type": existing.document_type,
                        "title": existing.title,
                        "published_at": existing.published_at.isoformat(),
                    }
                )
                continue
            payload = {
                **item,
                "security_id": security.id,
                "storage_path": archived.get("storage_path"),
                "parsed_text_path": archived.get("parsed_text_path"),
                "sha256": archived.get("sha256"),
                "size_bytes": archived.get("size_bytes"),
                "metadata": {
                    **dict(item.get("metadata") or {}),
                    "archive_final_url": archived.get("final_url"),
                    "parse_warning": archived.get("parse_warning"),
                },
            }
            row, created = self.repository.add_document(payload)
            created_count += int(created)
            documents.append(
                {
                    "document_id": row.id,
                    "external_id": row.external_id,
                    "created": created,
                    "archived": bool(archived),
                    "document_type": row.document_type,
                    "title": row.title,
                    "published_at": row.published_at.isoformat(),
                }
            )
        return {
            "security_id": security.id,
            "discovered": len(discovered),
            "created": created_count,
            "archived": archived_count,
            "documents": documents,
            "warnings": warnings,
        }
