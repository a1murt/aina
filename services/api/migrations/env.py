"""Alembic environment: async SQLAlchemy engine (asyncpg), URL from ``DATABASE_URL``."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from twin_core.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Models live in twin_core.db (shared by api, engine, collector). TimescaleDB objects (hypertables,
# compression/retention policies, continuous aggregates) are written by hand in the revisions.
target_metadata = Base.metadata
_TIMESCALE_VIEWS = frozenset({"telemetry_15m", "telemetry_1h"})


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object | None
) -> bool:
    """Keep autogenerate away from objects TimescaleDB manages (continuous aggregates)."""
    return not (type_ == "table" and name in _TIMESCALE_VIEWS)


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set (e.g. postgresql+asyncpg://qost:qost@localhost:5432/qost)"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, include_object=include_object
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
