from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text
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
