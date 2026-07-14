from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from backend.db.migrations import (
    SchemaCompatibilityError,
    initialize_schema,
)


def create_db_engine(database_url: str) -> Engine:
    kwargs = {"future": True}
    if database_url == "sqlite:///:memory:":
        kwargs.update(
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    elif database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(database_url, **kwargs)
    if database_url.startswith("sqlite"):
        event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def initialize_database(engine: Engine) -> None:
    with engine.begin() as connection:
        initialize_schema(connection)
    with engine.connect() as connection:
        if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 1:
            raise SchemaCompatibilityError("SQLite foreign keys are disabled")
        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        if violations:
            raise SchemaCompatibilityError("SQLite foreign key violations detected")


def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
