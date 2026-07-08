from sqlalchemy import create_engine, insert, select
from sqlalchemy.engine import Engine

from backend.db.schema import metadata, schema_version

CURRENT_SCHEMA_VERSION = 4


def create_db_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, connect_args=connect_args)


def initialize_database(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as connection:
        existing = connection.execute(
            select(schema_version.c.version).where(
                schema_version.c.version == CURRENT_SCHEMA_VERSION
            )
        ).scalar_one_or_none()
        if existing is None:
            connection.execute(
                insert(schema_version).values(version=CURRENT_SCHEMA_VERSION)
            )
