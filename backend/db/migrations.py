from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import delete, insert, inspect, select
from sqlalchemy.engine import Connection

from backend.db.schema import (
    agent_executions,
    agent_findings,
    context_facts,
    coordination_reviews,
    diagnosis_plans,
    diagnosis_tasks,
    memory_items,
    metadata,
    react_traces,
    runtime_attempts,
    runtime_checkpoints,
    runtime_events,
    runtime_runs,
    schema_version,
    tool_calls,
)

CURRENT_SCHEMA_VERSION = 6
Migration = Callable[[Connection], None]

_V3_TABLES = {
    "events",
    "evidence_items",
    "hypotheses",
    "investigations",
    "llm_analyses",
    "provider_results",
    "recommended_actions",
    "reports",
    "specialist_results",
    "verification_suggestions",
}
_V4_ADDITIONS = {
    "agent_executions",
    "context_facts",
    "diagnosis_plans",
    "diagnosis_tasks",
    "memory_items",
    "tool_calls",
}
_V5_ADDITIONS = {"agent_findings", "coordination_reviews", "react_traces"}
_V6_ADDITIONS = {
    "runtime_attempts",
    "runtime_checkpoints",
    "runtime_events",
    "runtime_runs",
}


class SchemaCompatibilityError(RuntimeError):
    """表示数据库声明的版本与实际结构不一致，禁止猜测修复。"""


def migrate_v3_to_v4(connection: Connection) -> None:
    """创建历史 V4 Agent process 表与索引。"""
    for table in (
        diagnosis_plans,
        diagnosis_tasks,
        agent_executions,
        context_facts,
        tool_calls,
        memory_items,
    ):
        table.create(connection)


def migrate_v4_to_v5(connection: Connection) -> None:
    """创建当前 V5-V7 只读兼容与 RCA workbench 表。"""
    for table in (agent_findings, coordination_reviews, react_traces):
        table.create(connection)


def migrate_v5_to_v6(connection: Connection) -> None:
    """创建 V9 Runtime 表；历史 Investigation 不生成伪造运行记录。"""
    for table in (runtime_runs, runtime_attempts, runtime_events, runtime_checkpoints):
        table.create(connection)


MIGRATIONS: dict[int, Migration] = {
    3: migrate_v3_to_v4,
    4: migrate_v4_to_v5,
    5: migrate_v5_to_v6,
}


def initialize_schema(connection: Connection) -> None:
    """初始化 fresh DB 或在一个事务内迁移受支持的历史 schema。"""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    application_tables = tables - {schema_version.name}
    if not application_tables and schema_version.name not in tables:
        metadata.create_all(connection)
        connection.execute(insert(schema_version).values(version=CURRENT_SCHEMA_VERSION))
        _validate_physical_schema(connection, CURRENT_SCHEMA_VERSION)
        return
    if schema_version.name not in tables:
        raise SchemaCompatibilityError("existing database has no schema_version")

    versions = set(connection.execute(select(schema_version.c.version)).scalars())
    if versions not in ({3}, {4}, {3, 4}, {5}, {6}):
        raise SchemaCompatibilityError(f"unsupported schema version set: {sorted(versions)}")
    current = max(versions)
    if current == 6:
        _migrate_legacy_v6_runtime_timeout(connection)
    _validate_physical_schema(connection, current, validate_indexes=False)
    while current < CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(current)
        if migration is None:
            raise SchemaCompatibilityError(f"no migration from schema version {current}")
        migration(connection)
        current += 1

    _validate_physical_schema(connection, CURRENT_SCHEMA_VERSION)
    connection.execute(delete(schema_version))
    connection.execute(insert(schema_version).values(version=CURRENT_SCHEMA_VERSION))


def _migrate_legacy_v6_runtime_timeout(connection: Connection) -> None:
    """补齐早期 V9 开发版 V6 Run；新列保留既有行且不改版本号。"""
    inspector = inspect(connection)
    if runtime_runs.name not in inspector.get_table_names():
        return
    columns = {
        column["name"] for column in inspector.get_columns(runtime_runs.name)
    }
    if "timeout_seconds" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE runtime_runs "
            "ADD COLUMN timeout_seconds FLOAT NOT NULL DEFAULT 60"
        )
    if "frozen_business_projection" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE runtime_runs ADD COLUMN frozen_business_projection JSON"
        )
    if "benchmark_replay_locator" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE runtime_runs ADD COLUMN benchmark_replay_locator JSON"
        )


def _validate_physical_schema(
    connection: Connection,
    version: int,
    *,
    validate_indexes: bool = True,
) -> None:
    required = set(_V3_TABLES)
    if version >= 4:
        required.update(_V4_ADDITIONS)
    if version >= 5:
        required.update(_V5_ADDITIONS)
    if version >= 6:
        required.update(_V6_ADDITIONS)
    inspector = inspect(connection)
    actual_tables = set(inspector.get_table_names())
    missing_tables = required - actual_tables
    if missing_tables:
        raise SchemaCompatibilityError(
            f"schema {version} missing tables: {sorted(missing_tables)}"
        )

    for table_name in required:
        expected_table = metadata.tables[table_name]
        actual_columns = {
            item["name"] for item in inspector.get_columns(table_name)
        }
        expected_columns = {column.name for column in expected_table.columns}
        if not expected_columns <= actual_columns:
            raise SchemaCompatibilityError(
                f"table {table_name} missing columns: "
                f"{sorted(expected_columns - actual_columns)}"
            )
        expected_targets = {
            foreign_key.target_fullname.split(".", 1)[0]
            for foreign_key in expected_table.foreign_keys
        }
        actual_targets = {
            item["referred_table"] for item in inspector.get_foreign_keys(table_name)
        }
        if expected_targets != actual_targets:
            raise SchemaCompatibilityError(
                f"table {table_name} foreign keys do not match manifest"
            )
        if not validate_indexes:
            continue
        expected_indexes = {
            index.name for index in expected_table.indexes if index.name is not None
        }
        actual_indexes = {
            item["name"] for item in inspector.get_indexes(table_name)
        }
        if not expected_indexes <= actual_indexes:
            raise SchemaCompatibilityError(
                f"table {table_name} missing indexes: "
                f"{sorted(expected_indexes - actual_indexes)}"
            )
