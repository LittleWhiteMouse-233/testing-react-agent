from __future__ import annotations

from sqlalchemy import event
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


async def init_database(engine: AsyncEngine) -> None:
    # Alembic remains the versioned schema source. create_all keeps a fresh
    # development checkout immediately runnable.
    from app.persistence import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

