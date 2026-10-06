"""Async database access for the API (SQLAlchemy 2 + asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qost_api.problems import ProblemError


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory: async_sessionmaker[AsyncSession] | None = request.app.state.sessionmaker
    if factory is None:
        raise ProblemError(
            503, "Database is not configured", "DATABASE_URL is not set", slug="no-database"
        )
    async with factory() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
