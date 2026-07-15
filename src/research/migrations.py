# -*- coding: utf-8 -*-
"""Ordered, idempotent schema migrations for the independent Research DB."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import Engine, insert, select

from src.research.models import ResearchBase, ResearchSchemaMigration, utc_now_naive


RESEARCH_SCHEMA_VERSION = "2026-07-15-research-v1"


@dataclass(frozen=True)
class ResearchMigration:
    version: str
    description: str
    apply: Callable[[object], None]


def _create_initial_schema(connection) -> None:
    ResearchBase.metadata.create_all(connection)


RESEARCH_MIGRATIONS = (
    ResearchMigration(
        version=RESEARCH_SCHEMA_VERSION,
        description="Initial independent PEI research domain schema",
        apply=_create_initial_schema,
    ),
)


def run_research_migrations(engine: Engine) -> None:
    """Apply every missing migration in order inside explicit transactions."""
    ResearchSchemaMigration.__table__.create(engine, checkfirst=True)
    with engine.begin() as connection:
        applied = set(connection.scalars(select(ResearchSchemaMigration.version)))
    for migration in RESEARCH_MIGRATIONS:
        if migration.version in applied:
            continue
        with engine.begin() as connection:
            migration.apply(connection)
            connection.execute(
                insert(ResearchSchemaMigration).values(
                    version=migration.version,
                    description=migration.description,
                    applied_at=utc_now_naive(),
                )
            )
        applied.add(migration.version)
