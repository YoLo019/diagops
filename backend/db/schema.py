from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
)
from sqlalchemy.types import JSON

metadata = MetaData()

schema_version = Table(
    "schema_version",
    metadata,
    Column("version", Integer, primary_key=True),
)

investigations = Table(
    "investigations",
    metadata,
    Column("id", String, primary_key=True),
    Column("event", JSON, nullable=False),
    Column("status", String, nullable=False),
    Column("failure_reason", Text, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Column("completed_at", String, nullable=True),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

evidence_items = Table(
    "evidence_items",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

hypotheses = Table(
    "hypotheses",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

recommended_actions = Table(
    "recommended_actions",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

verification_suggestions = Table(
    "verification_suggestions",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

reports = Table(
    "reports",
    metadata,
    Column("investigation_id", ForeignKey("investigations.id"), primary_key=True),
    Column("payload", JSON, nullable=False),
)

provider_results = Table(
    "provider_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

specialist_results = Table(
    "specialist_results",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("payload", JSON, nullable=False),
)

llm_analyses = Table(
    "llm_analyses",
    metadata,
    Column("investigation_id", ForeignKey("investigations.id"), primary_key=True),
    Column("payload", JSON, nullable=False),
)

diagnosis_plans = Table(
    "diagnosis_plans",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

diagnosis_tasks = Table(
    "diagnosis_tasks",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("position", Integer, nullable=False),
    Column("status", String, nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

agent_executions = Table(
    "agent_executions",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("task_id", String, nullable=False, index=True),
    Column("status", String, nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

context_facts = Table(
    "context_facts",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

tool_calls = Table(
    "tool_calls",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("task_id", String, nullable=False, index=True),
    Column("status", String, nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

memory_items = Table(
    "memory_items",
    metadata,
    Column("id", String, primary_key=True),
    Column("service", String, nullable=False, index=True),
    Column("environment", String, nullable=False, index=True),
    Column("source_investigation_id", ForeignKey("investigations.id"), nullable=True, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

agent_findings = Table(
    "agent_findings",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("agent_name", String, nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

coordination_reviews = Table(
    "coordination_reviews",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

react_traces = Table(
    "react_traces",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("payload", JSON, nullable=False),
    Column("created_at", String, nullable=False, index=True),
)

runtime_runs = Table(
    "runtime_runs",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False),
    Column("run_kind", String, nullable=False),
    Column("strategy", String, nullable=False),
    Column("status", String, nullable=False),
    Column("current_phase", String, nullable=True),
    Column("source_run_id", ForeignKey("runtime_runs.id"), nullable=True),
    Column("parent_run_id", ForeignKey("runtime_runs.id"), nullable=True),
    Column("run_reason", String, nullable=False),
    Column("model_provider", String, nullable=True),
    Column("model_name", String, nullable=True),
    Column("prompt_version", String, nullable=True),
    Column("tool_budget", Integer, nullable=True),
    Column("token_budget", Integer, nullable=True),
    Column("timeout_seconds", Float, nullable=False, server_default="60"),
    Column("latest_checkpoint_id", ForeignKey("runtime_checkpoints.id"), nullable=True),
    Column("failure_category", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("started_at", String, nullable=True),
    Column("completed_at", String, nullable=True),
    Column("cancel_requested_at", String, nullable=True),
    Column("lease_owner", String, nullable=True),
    Column("lease_expires_at", String, nullable=True),
    Column("lease_version", Integer, nullable=False, default=0),
    Column("next_event_sequence", Integer, nullable=False, default=0),
    Column("frozen_business_projection", JSON, nullable=True),
    Column("benchmark_replay_locator", JSON, nullable=True),
)

runtime_attempts = Table(
    "runtime_attempts",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", ForeignKey("runtime_runs.id"), nullable=False),
    Column("attempt_number", Integer, nullable=False),
    Column(
        "resume_from_checkpoint_id",
        ForeignKey("runtime_checkpoints.id"),
        nullable=True,
    ),
    Column("status", String, nullable=False),
    Column("started_at", String, nullable=False),
    Column("completed_at", String, nullable=True),
    Column("failure_category", String, nullable=True),
    # OTel 链接标识仅用于内部恢复关联，不进入公共 Runtime 合约。
    Column("trace_id", String, nullable=True),
    Column("root_span_id", String, nullable=True),
    UniqueConstraint("run_id", "attempt_number", name="uq_runtime_attempt_run_number"),
)

runtime_events = Table(
    "runtime_events",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", ForeignKey("runtime_runs.id"), nullable=False),
    Column("attempt_id", ForeignKey("runtime_attempts.id"), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("event_type", String, nullable=False),
    Column("phase", String, nullable=True),
    Column("actor_type", String, nullable=False),
    Column("actor_name", String, nullable=True),
    Column("task_id", String, nullable=True),
    Column("execution_id", String, nullable=True),
    Column("tool_call_id", String, nullable=True),
    Column("evidence_ids", JSON, nullable=False),
    Column("safe_payload", JSON, nullable=False),
    Column("occurred_at", String, nullable=False),
    Column("schema_version", Integer, nullable=False),
    UniqueConstraint("run_id", "sequence", name="uq_runtime_event_run_sequence"),
)

runtime_checkpoints = Table(
    "runtime_checkpoints",
    metadata,
    Column("id", String, primary_key=True),
    Column("run_id", ForeignKey("runtime_runs.id"), nullable=False),
    Column("attempt_id", ForeignKey("runtime_attempts.id"), nullable=False),
    Column("completed_phase", String, nullable=False),
    Column("event_sequence", Integer, nullable=False),
    Column("state_digest", String, nullable=False),
    Column("projection_digest", String, nullable=False, default="0" * 64),
    Column("resume_state", JSON, nullable=False),
    Column("created_at", String, nullable=False),
    Column("schema_version", Integer, nullable=False),
)

Index(
    "ix_runtime_runs_investigation_created_at",
    runtime_runs.c.investigation_id,
    runtime_runs.c.created_at,
)
Index("ix_runtime_runs_lease_expires_at", runtime_runs.c.lease_expires_at)
Index(
    "uq_runtime_runs_one_active_live_per_investigation",
    runtime_runs.c.investigation_id,
    unique=True,
    sqlite_where=and_(
        runtime_runs.c.run_kind == "live",
        runtime_runs.c.status.in_(("created", "running", "cancelling", "interrupted")),
    ),
)
Index("ix_runtime_attempts_run_id", runtime_attempts.c.run_id)
Index(
    "uq_runtime_attempts_one_active_per_run",
    runtime_attempts.c.run_id,
    unique=True,
    sqlite_where=runtime_attempts.c.status == "running",
)
Index(
    "ix_runtime_events_run_sequence",
    runtime_events.c.run_id,
    runtime_events.c.sequence,
)
Index("ix_runtime_checkpoints_run_id", runtime_checkpoints.c.run_id)

Index(
    "ix_memory_items_service_environment_created_at",
    memory_items.c.service,
    memory_items.c.environment,
    memory_items.c.created_at,
)

Index(
    "ix_agent_findings_investigation_agent_created_at",
    agent_findings.c.investigation_id,
    agent_findings.c.agent_name,
    agent_findings.c.created_at,
)
