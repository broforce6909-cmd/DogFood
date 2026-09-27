"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# `pool_size`/`max_overflow` are explicit rather than SQLAlchemy's defaults
# (5 + 10 = 15 connections total): a load test at 500+ concurrent judges/
# voters (see ARCHITECTURE.md's "Performance" section) found *this* -- not any
# single query -- was the actual ceiling on concurrent throughput. Every sync
# route already runs on Starlette's own worker-thread pool (default 40
# threads), so a request past the 15th was never CPU-starved; it was queued
# waiting for a database connection that 14 other requests already held,
# which is exactly the multi-second-tail-latency-with-zero-errors signature a
# load test at realistic concurrency (and only a load test at realistic
# concurrency) surfaces. 40 total matches that same thread-pool ceiling, so
# the database stops being the tighter of the two limits without being sized
# past what a single process can actually put concurrently in flight.
engine = create_engine(
    settings.database_url, pool_pre_ping=True, pool_size=20, max_overflow=20, future=True
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base for every ORM model."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
