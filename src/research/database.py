# -*- coding: utf-8 -*-
"""Lifecycle-managed database connection for the independent research domain."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.research.migrations import RESEARCH_SCHEMA_VERSION, run_research_migrations


__all__ = ["RESEARCH_SCHEMA_VERSION", "ResearchDatabase"]


def _research_db_url(path: Optional[Path] = None) -> str:
    if path is not None:
        return f"sqlite:///{path.expanduser().resolve()}"
    configured = (os.getenv("RESEARCH_DATABASE_PATH") or "./data/research/research.db").strip()
    resolved = Path(configured).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    return f"sqlite:///{resolved.resolve()}"


class ResearchDatabase:
    """Own one Research DB engine without touching the DSA core database."""

    def __init__(self, *, db_url: Optional[str] = None, path: Optional[Path] = None) -> None:
        self.db_url = db_url or _research_db_url(path)
        engine_kwargs: dict = {"pool_pre_ping": True}
        if self.db_url.startswith("sqlite:///:memory:"):
            engine_kwargs.update(
                {
                    "connect_args": {"check_same_thread": False},
                    "poolclass": StaticPool,
                }
            )
        elif self.db_url.startswith("sqlite:"):
            database_path = Path(self.db_url.removeprefix("sqlite:///")).expanduser()
            database_path.parent.mkdir(parents=True, exist_ok=True)
            engine_kwargs["connect_args"] = {
                "check_same_thread": False,
                "timeout": max(1, int(os.getenv("RESEARCH_SQLITE_BUSY_TIMEOUT_MS", "10000"))) / 1000,
            }
        self.engine = create_engine(self.db_url, **engine_kwargs)
        if self.engine.url.get_backend_name() == "sqlite":
            event.listen(self.engine, "connect", self._set_sqlite_pragmas)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        run_research_migrations(self.engine)

    @staticmethod
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()
