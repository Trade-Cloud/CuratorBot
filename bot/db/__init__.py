import logging

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from .. import texts
from .models import Base

log = logging.getLogger(__name__)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    # For now the schema is created with create_all. Switch to alembic once the structure settles.
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


def _add_missing_columns(sync_conn) -> None:
    """Tiny migration: create_all does not add new columns to existing tables, so we do it ourselves.
    Nullable columns without server defaults only; anything more complex goes through alembic."""
    inspector = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing or not column.nullable:
                continue
            ddl_type = column.type.compile(dialect=sync_conn.dialect)
            sync_conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}')
            log.info(texts.LOG["db"]["added_column"], table.name, column.name)


__all__ = ["Base", "init_db", "make_engine", "make_sessionmaker"]
