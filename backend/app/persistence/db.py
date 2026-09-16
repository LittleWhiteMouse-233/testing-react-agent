from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.engine.interfaces import DBAPIConnection


class Base(DeclarativeBase):
    pass


def build_engine(database_url: str) -> AsyncEngine:
    engine = create_async_engine(database_url, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection: DBAPIConnection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def build_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def read_snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Own one read response's SQLite snapshot without changing write policy.

    The aiosqlite legacy transaction mode does not BEGIN for SELECT. Emit the
    documented explicit BEGIN at this read-only boundary; session close rolls
    it back. Callers must finish all related reads before leaving the context.
    """
    async with sessions() as session:
        await session.execute(text("BEGIN"))
        yield session


async def init_database(engine: AsyncEngine) -> None:
    # Alembic remains the versioned schema source. create_all keeps a fresh
    # development checkout immediately runnable.
    from app.persistence import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
