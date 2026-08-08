from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.domain.multi_agent import (
    AuthorityMode,
    ExecutionContractVersion,
    InvestigationStrategy,
    ModelProvider,
)
from backend.safety.redaction import redact_text, redact_value


class RuntimeRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    CANCELLING = "cancelling"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimePhase(StrEnum):
    INTAKE = "intake"
    EVIDENCE_COLLECTION = "evidence_collection"
    DETERMINISTIC_RCA = "deterministic_rca"
    SPECIALIST_ANALYSIS = "specialist_analysis"
    CONFLICT_REVIEW = "conflict_review"
    COORDINATION = "coordination"
    REPORT_GENERATION = "report_generation"
    FINALIZE = "finalize"
    LEAD_PLANNING = "lead_planning"
    INVESTIGATOR_ROUND_1 = "investigator_round_1"
    CRITIC_REVIEW = "critic_review"
    INVESTIGATOR_ROUND_2 = "investigator_round_2"
    CRITIC_RECONCILIATION = "critic_reconciliation"
    LEAD_ADJUDICATION = "lead_adjudication"
    RESULT_VALIDATION = "result_validation"


class RuntimeRunKind(StrEnum):
    LIVE = "live"
    REPLAY = "replay"


class RuntimeRunReason(StrEnum):
    INITIAL = "initial"
    ADDITIONAL_EVIDENCE = "additional_evidence"
    MANUAL_RERUN = "manual_rerun"
    REPLAY = "replay"


class RuntimeAttemptStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


ALLOWED_ATTEMPT_TRANSITIONS: dict[
    RuntimeAttemptStatus, frozenset[RuntimeAttemptStatus]
] = {
    RuntimeAttemptStatus.RUNNING: frozenset(
        {
            RuntimeAttemptStatus.COMPLETED,
            RuntimeAttemptStatus.FAILED,
            RuntimeAttemptStatus.CANCELLED,
            RuntimeAttemptStatus.INTERRUPTED,
        }
    ),
    RuntimeAttemptStatus.COMPLETED: frozenset(),
    RuntimeAttemptStatus.FAILED: frozenset(),
    RuntimeAttemptStatus.CANCELLED: frozenset(),
    RuntimeAttemptStatus.INTERRUPTED: frozenset(),
}


def ensure_attempt_transition(
    source: RuntimeAttemptStatus, target: RuntimeAttemptStatus
) -> None:
    """校验 Attempt 只能从 running 进入一个不可变终态。"""
    if target not in ALLOWED_ATTEMPT_TRANSITIONS[source]:
        raise ValueError(f"invalid attempt transition: {source.value} -> {target.value}")


class RuntimeActorType(StrEnum):
    RUNTIME = "runtime"
    PHASE = "phase"
    AGENT = "agent"
    MODEL = "model"
    TOOL = "tool"
    PROVIDER = "provider"
    RECOVERY = "recovery"


class RuntimeEventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_INTERRUPTED = "run.interrupted"
    RUN_CANCEL_REQUESTED = "run.cancel_requested"
    RUN_CANCELLED = "run.cancelled"
    ATTEMPT_STARTED = "attempt.started"
    ATTEMPT_COMPLETED = "attempt.completed"
    ATTEMPT_INTERRUPTED = "attempt.interrupted"
    PHASE_STARTED = "phase.started"
    PHASE_COMPLETED = "phase.completed"
    PHASE_FAILED = "phase.failed"
    PHASE_SKIPPED = "phase.skipped"
    AGENT_STARTED = "agent.started"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_PROPOSED = "tool.proposed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_REJECTED = "tool.rejected"
    TOOL_SKIPPED = "tool.skipped"
    EVIDENCE_PERSISTED = "evidence.persisted"
    EVIDENCE_REJECTED = "evidence.rejected"
    CHECKPOINT_CREATED = "checkpoint.created"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_COMPLETED = "recovery.completed"
    RECOVERY_REJECTED = "recovery.rejected"


class RuntimeFailureCategory(StrEnum):
    NONE = "none"
    LEASE_LOST = "lease_lost"
    CHECKPOINT_INVALID = "checkpoint_invalid"
    RESUME_CONFLICT = "resume_conflict"
    PERSISTENCE_FAILURE = "persistence_failure"
    CANCELLED = "cancelled"
    PROVIDER_FAILURE = "provider_failure"
    MODEL_FAILURE = "model_failure"
    TIMEOUT = "timeout"
    OUTPUT_VALIDATION = "output_validation"
    CONTRACT_INTEGRITY = "contract_integrity"
    UNKNOWN = "unknown"


class RunOwnership(BaseModel):
    investigation_id: str = Field(min_length=1)
    runtime_run_id: str = Field(min_length=1)


_EXECUTION_CONTRACT_DIGEST = "execution_contract_digest"
_V11_CONTRACT_REQUIRED_KEYS = {
    "execution_contract_version",
    "authority_mode",
    "model_provider",
    "model_name",
    "prompt_version",
    "api_mode",
    "endpoint_id",
    "capability_artifact_hash",
    "tool_manifest",
    "tool_manifest_hash",
    "skill_catalog",
    "capability_identity",
    "limits",
    "retry_policy",
    "tool_budget",
    "token_budget",
    "timeout_seconds",
    _EXECUTION_CONTRACT_DIGEST,
}
_V11_CAPABILITY_KEYS = {
    "provider",
    "model",
    "api_mode",
    "endpoint_id",
    "artifact_hash",
}
_V11_LIMIT_KEYS = {
    "max_turns",
    "max_investigators",
    "max_rounds",
    "token_budget",
    "max_tool_calls_per_specialist",
    "tool_timeout_seconds",
}


def execution_contract_digest(contract: dict[str, Any]) -> str:
    """返回不含凭据且排除自身字段的 execution contract 稳定摘要。"""
    payload = {
        key: value
        for key, value in contract.items()
        if key != _EXECUTION_CONTRACT_DIGEST
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_v11_execution_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """由服务端为 V11 admission 生成带摘要的不可变契约快照。"""
    sealed = deepcopy(contract)
    sealed[_EXECUTION_CONTRACT_DIGEST] = execution_contract_digest(sealed)
    return sealed


def validate_v11_execution_contract(contract: dict[str, Any]) -> None:
    """校验 V11 nested contract 的完整性、摘要和可执行边界。"""
    if not isinstance(contract, dict) or not _V11_CONTRACT_REQUIRED_KEYS <= contract.keys():
        raise ValueError("V11 execution contract is incomplete")
    if contract.get(_EXECUTION_CONTRACT_DIGEST) != execution_contract_digest(contract):
        raise ValueError("V11 execution contract digest mismatch")
    if contract.get("execution_contract_version") != ExecutionContractVersion.V11.value:
        raise ValueError("V11 execution contract version mismatch")
    if contract.get("authority_mode") != AuthorityMode.AGENT.value:
        raise ValueError("V11 execution contract authority mismatch")
    capability = contract.get("capability_identity")
    limits = contract.get("limits")
    retry_policy = contract.get("retry_policy")
    skill_catalog = contract.get("skill_catalog")
    if not isinstance(capability, dict) or not _V11_CAPABILITY_KEYS <= capability.keys():
        raise ValueError("V11 execution contract capability identity is incomplete")
    if not isinstance(limits, dict) or not _V11_LIMIT_KEYS <= limits.keys():
        raise ValueError("V11 execution contract limits are incomplete")
    if not isinstance(skill_catalog, dict):
        raise ValueError("V11 execution contract skill catalog is incomplete")
    if not isinstance(retry_policy, dict):
        raise ValueError("V11 execution contract retry policy is incomplete")
    if contract.get("token_budget") is None or limits.get("token_budget") is None:
        raise ValueError("V11 execution contract token ceiling is missing")
    if limits["token_budget"] != contract["token_budget"]:
        raise ValueError("V11 execution contract token ceiling mismatch")
    if contract.get("max_tool_calls_per_specialist") is not None:
        raise ValueError("V11 specialist limit must be nested under limits")
    if retry_policy.get("max_retries") != 1:
        raise ValueError("V11 retry policy max_retries must be one")
    if set(retry_policy.get("retryable_categories", ())) != {"transport", "rate_limit"}:
        raise ValueError("V11 retry policy categories are not frozen")
    if retry_policy.get("provider_max_retries") != 0:
        raise ValueError("V11 provider retries must be disabled")
    if retry_policy.get("sdk_max_retries") != 0:
        raise ValueError("V11 SDK retries must be disabled")


def _validate_contract_value(value: Any, path: str = "execution_contract") -> None:
    if value is None or isinstance(value, (str, int, bool)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > 64:
            raise ValueError(f"{path} exceeds the maximum item count")
        for index, item in enumerate(value):
            _validate_contract_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError(f"{path} exceeds the maximum item count")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError(f"{path} contains an invalid key")
            if key.lower().replace("-", "_") in {
                "api_key",
                "authorization",
                "credential",
                "password",
                "secret",
                "prompt",
                "reasoning",
            }:
                raise ValueError(f"{path} contains prohibited key: {key}")
            _validate_contract_value(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains an unsupported value")


ALLOWED_TRANSITIONS: dict[RuntimeRunStatus, frozenset[RuntimeRunStatus]] = {
    RuntimeRunStatus.CREATED: frozenset(
        {RuntimeRunStatus.RUNNING, RuntimeRunStatus.CANCELLED}
    ),
    RuntimeRunStatus.RUNNING: frozenset(
        {
            RuntimeRunStatus.COMPLETED,
            RuntimeRunStatus.FAILED,
            RuntimeRunStatus.CANCELLING,
            RuntimeRunStatus.INTERRUPTED,
        }
    ),
    RuntimeRunStatus.CANCELLING: frozenset(
        {RuntimeRunStatus.CANCELLED, RuntimeRunStatus.INTERRUPTED}
    ),
    RuntimeRunStatus.INTERRUPTED: frozenset({RuntimeRunStatus.RUNNING}),
    RuntimeRunStatus.COMPLETED: frozenset(),
    RuntimeRunStatus.FAILED: frozenset(),
    RuntimeRunStatus.CANCELLED: frozenset(),
}


def ensure_run_transition(source: RuntimeRunStatus, target: RuntimeRunStatus) -> None:
    """校验 Runtime Run 状态迁移，避免终态或非法跳转被持久化。"""
    if target not in ALLOWED_TRANSITIONS[source]:
        raise ValueError(f"invalid runtime transition: {source.value} -> {target.value}")


class RuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RuntimeRun(RuntimeModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    investigation_id: str
    run_kind: RuntimeRunKind
    strategy: InvestigationStrategy
    status: RuntimeRunStatus = RuntimeRunStatus.CREATED
    current_phase: RuntimePhase | None = None
    source_run_id: str | None = None
    parent_run_id: str | None = None
    run_reason: RuntimeRunReason
    model_provider: ModelProvider | None = None
    model_name: str | None = Field(default=None, max_length=160)
    prompt_version: str | None = Field(default=None, max_length=160)
    tool_budget: int | None = Field(default=None, ge=0)
    token_budget: int | None = Field(default=None, ge=0)
    timeout_seconds: float = Field(
        default=60.0, ge=1, allow_inf_nan=False
    )
    latest_checkpoint_id: str | None = None
    failure_category: RuntimeFailureCategory | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    completed_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    lease_version: int = Field(default=0, ge=0)
    execution_contract_version: ExecutionContractVersion = ExecutionContractVersion.V10_LEGACY
    authority_mode: AuthorityMode = AuthorityMode.LEGACY_DETERMINISTIC
    execution_contract: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model_name", "prompt_version")
    @classmethod
    def validate_public_metadata(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            _SAFE_IDENTIFIER.fullmatch(value) is None
            or "://" in value
            or redact_text(value) != value
        ):
            raise ValueError("runtime metadata must be a safe bounded identifier")
        return value

    @model_validator(mode="after")
    def validate_lineage(self) -> RuntimeRun:
        if self.run_kind == RuntimeRunKind.REPLAY:
            if (
                self.run_reason != RuntimeRunReason.REPLAY
                or self.source_run_id is None
                or self.parent_run_id is not None
            ):
                raise ValueError("replay runs require only source_run_id")
            return self
        if self.run_reason == RuntimeRunReason.REPLAY or self.source_run_id is not None:
            raise ValueError("live runs cannot use replay lineage")
        if self.run_reason == RuntimeRunReason.INITIAL:
            if self.parent_run_id is not None:
                raise ValueError("initial runs cannot have a parent")
        elif self.parent_run_id is None:
            raise ValueError("additional and manual runs require a parent")
        return self

    @model_validator(mode="after")
    def validate_execution_identity(self) -> RuntimeRun:
        if not self.execution_contract:
            self.execution_contract = self._legacy_contract()
        _validate_contract_value(self.execution_contract)
        if self.execution_contract_version == ExecutionContractVersion.V11:
            if self.authority_mode != AuthorityMode.AGENT:
                raise ValueError("V11 runs require agent authority")
            if self.token_budget is None or self.execution_contract.get("token_budget") is None:
                raise ValueError("V11 runs require a non-None token_budget")
            required = {
                "model_provider",
                "model_name",
                "prompt_version",
                "tool_budget",
                "token_budget",
                "timeout_seconds",
            }
            if not required <= self.execution_contract.keys():
                raise ValueError("V11 execution contract is incomplete")
            if self.timeout_seconds > 120:
                raise ValueError("V11 timeout_seconds exceeds the hard deadline")
            if _EXECUTION_CONTRACT_DIGEST in self.execution_contract:
                try:
                    validate_v11_execution_contract(self.execution_contract)
                except ValueError as exc:
                    raise ValueError(str(exc)) from exc
        elif self.authority_mode != AuthorityMode.LEGACY_DETERMINISTIC:
            raise ValueError("legacy execution contracts require legacy authority")
        contract_version = self.execution_contract.get("execution_contract_version")
        if (
            contract_version is not None
            and contract_version != self.execution_contract_version.value
        ):
            raise ValueError("execution contract version mismatch")
        contract_authority = self.execution_contract.get("authority_mode")
        if contract_authority is not None and contract_authority != self.authority_mode.value:
            raise ValueError("execution contract authority mismatch")
        for field_name in (
            "model_provider",
            "model_name",
            "prompt_version",
            "tool_budget",
            "token_budget",
            "timeout_seconds",
        ):
            if field_name in self.execution_contract:
                expected = getattr(self, field_name)
                actual = self.execution_contract[field_name]
                if isinstance(expected, StrEnum):
                    expected = expected.value
                if actual != expected:
                    raise ValueError(f"execution contract projection mismatch: {field_name}")
        return self

    def _legacy_contract(self) -> dict[str, Any]:
        return {
            "execution_contract_version": ExecutionContractVersion.V10_LEGACY.value,
            "authority_mode": AuthorityMode.LEGACY_DETERMINISTIC.value,
            "run_kind": self.run_kind.value,
            "run_reason": self.run_reason.value,
            "strategy": self.strategy.value,
            "model_provider": self.model_provider.value if self.model_provider else None,
            "model_name": self.model_name,
            "prompt_version": self.prompt_version,
            "tool_budget": self.tool_budget,
            "token_budget": self.token_budget,
            "timeout_seconds": self.timeout_seconds,
        }

    @property
    def is_v11(self) -> bool:
        return self.execution_contract_version == ExecutionContractVersion.V11


class RuntimeAttempt(RuntimeModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    attempt_number: int = Field(ge=1)
    resume_from_checkpoint_id: str | None = None
    status: RuntimeAttemptStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    failure_category: RuntimeFailureCategory | None = None


class RuntimeResumeState(RuntimeModel):
    completed_evidence_ids: list[str] = Field(default_factory=list)
    completed_finding_ids: list[str] = Field(default_factory=list)
    completed_review_ids: list[str] = Field(default_factory=list)
    completed_report_ids: list[str] = Field(default_factory=list)
    remaining_tool_budget: int = Field(default=0, ge=0)
    remaining_token_budget: int | None = Field(default=None, ge=0)
    successful_tool_keys: list[str] = Field(default_factory=list)


_COMMON_PAYLOAD_KEYS = frozenset(
    {
        "status",
        "duration_ms",
        "failure_category",
        "safe_code",
        "message",
    }
)
_EVENT_PAYLOAD_KEYS: dict[RuntimeEventType, frozenset[str]] = {
    event_type: _COMMON_PAYLOAD_KEYS for event_type in RuntimeEventType
}
for _event_type in (
    RuntimeEventType.RUN_CREATED,
    RuntimeEventType.RUN_STARTED,
    RuntimeEventType.RUN_COMPLETED,
    RuntimeEventType.RUN_FAILED,
    RuntimeEventType.RUN_INTERRUPTED,
    RuntimeEventType.RUN_CANCEL_REQUESTED,
    RuntimeEventType.RUN_CANCELLED,
):
    _EVENT_PAYLOAD_KEYS[_event_type] |= frozenset(
        {"run_kind", "run_reason", "strategy", "provider", "model", "prompt_version"}
    )
for _event_type in (
    RuntimeEventType.PHASE_STARTED,
    RuntimeEventType.PHASE_COMPLETED,
    RuntimeEventType.PHASE_FAILED,
    RuntimeEventType.PHASE_SKIPPED,
):
    _EVENT_PAYLOAD_KEYS[_event_type] |= frozenset(
        {"evidence_count", "finding_count", "review_count", "report_count"}
    )
for _event_type in (
    RuntimeEventType.AGENT_STARTED,
    RuntimeEventType.AGENT_COMPLETED,
    RuntimeEventType.AGENT_FAILED,
    RuntimeEventType.MODEL_STARTED,
    RuntimeEventType.MODEL_COMPLETED,
    RuntimeEventType.MODEL_FAILED,
):
    _EVENT_PAYLOAD_KEYS[_event_type] |= frozenset(
        {"provider", "model", "input_tokens", "output_tokens", "cost"}
    )
for _event_type in (
    RuntimeEventType.TOOL_PROPOSED,
    RuntimeEventType.TOOL_STARTED,
    RuntimeEventType.TOOL_COMPLETED,
    RuntimeEventType.TOOL_FAILED,
    RuntimeEventType.TOOL_REJECTED,
    RuntimeEventType.TOOL_SKIPPED,
):
    _EVENT_PAYLOAD_KEYS[_event_type] |= frozenset(
        {"tool_name", "idempotency_key", "normalized_inputs", "metadata"}
    )
for _event_type in (
    RuntimeEventType.EVIDENCE_PERSISTED,
    RuntimeEventType.EVIDENCE_REJECTED,
):
    _EVENT_PAYLOAD_KEYS[_event_type] |= frozenset({"evidence_count", "evidence_kind"})
_EVENT_PAYLOAD_KEYS[RuntimeEventType.CHECKPOINT_CREATED] |= frozenset(
    {"checkpoint_id", "event_sequence", "state_digest", "projection_digest"}
)
for _event_type in (
    RuntimeEventType.RECOVERY_STARTED,
    RuntimeEventType.RECOVERY_COMPLETED,
    RuntimeEventType.RECOVERY_REJECTED,
):
    _EVENT_PAYLOAD_KEYS[_event_type] |= frozenset(
        {"checkpoint_id", "resume_attempt_number"}
    )

_PROHIBITED_PAYLOAD_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "chain_of_thought",
        "credential",
        "credentials",
        "evidence_body",
        "model_output",
        "password",
        "prompt",
        "provider_payload",
        "raw_output",
        "raw_payload",
        "reasoning",
        "request_payload",
        "response_payload",
        "secret",
    }
)


_MAX_SAFE_STRING_LENGTH = 512
_MAX_STRUCTURED_STRING_LENGTH = 256
_MAX_STRUCTURED_ITEMS = 32
_MAX_STRUCTURED_DEPTH = 4
_MAX_STRUCTURED_NODES = 128

_TOOL_NORMALIZED_INPUT_KEYS: dict[str, frozenset[str]] = {
    "read_logs": frozenset(
        {"start_time", "end_time", "limit", "keywords", "levels", "instance"}
    ),
    "query_metrics": frozenset(
        {
            "start_time",
            "end_time",
            "limit",
            "metric_names",
            "aggregation",
            "instance",
        }
    ),
    "query_prometheus": frozenset(
        {
            "start_time",
            "end_time",
            "limit",
            "metric_names",
            "aggregation",
            "instance",
        }
    ),
    "read_deployments": frozenset(
        {"start_time", "end_time", "limit", "version", "instance"}
    ),
    "read_service_catalog": frozenset(
        {"start_time", "end_time", "limit", "include_dependencies"}
    ),
    "query_dependencies": frozenset(
        {"start_time", "end_time", "limit", "direction", "target", "depth"}
    ),
    "lookup_memory": frozenset(),
}
_COMMON_TOOL_METADATA_KEYS = frozenset(
    {
        "duration_ms",
        "evidence_count",
        "provider_status",
        "result_count",
        "reused",
        "safe_code",
    }
)
_OPAQUE_EVENT_PAYLOAD_KEYS = _COMMON_PAYLOAD_KEYS | frozenset({"metadata"})
_TOOL_METADATA_KEYS = {
    tool_name: _COMMON_TOOL_METADATA_KEYS for tool_name in _TOOL_NORMALIZED_INPUT_KEYS
}
_TOOL_INTEGER_FIELDS = frozenset(
    {"depth", "duration_ms", "evidence_count", "limit", "result_count"}
)
_TOOL_BOOLEAN_FIELDS = frozenset({"include_dependencies", "reused"})
_TOOL_LIST_FIELDS = frozenset({"keywords", "levels", "metric_names"})
_TOOL_ENUM_FIELDS: dict[str, frozenset[str]] = {
    "aggregation": frozenset({"avg", "max", "sum"}),
    "direction": frozenset({"upstream", "downstream"}),
    "provider_status": frozenset(
        {"success", "failed", "partial", "skipped", "not_configured"}
    ),
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/+~-]{0,159}$")
_ISO_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _normalized_key_segments(key: str) -> tuple[list[str], str]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    segments = [item.lower() for item in re.split(r"[^a-zA-Z0-9]+", expanded) if item]
    return segments, "".join(segments)


def _is_prohibited_payload_key(key: str) -> bool:
    segments, compact = _normalized_key_segments(key)
    if set(segments) & {
        "authorization",
        "credential",
        "credentials",
        "password",
        "prompt",
        "reasoning",
        "secret",
    }:
        return True
    return compact in {
        "apikey",
        "chainofthought",
        "evidencebody",
        "modeloutput",
        "providerpayload",
        "rawoutput",
        "rawpayload",
        "requestpayload",
        "responsepayload",
    }


def _validate_json_value(
    value: Any,
    *,
    path: str = "safe_payload",
    depth: int = 0,
    structured: bool = False,
    budget: list[int] | None = None,
) -> None:
    budget = budget if budget is not None else [0]
    budget[0] += 1
    if budget[0] > _MAX_STRUCTURED_NODES:
        raise ValueError(f"{path} exceeds maximum total size")
    if depth > _MAX_STRUCTURED_DEPTH:
        raise ValueError(f"{path} exceeds maximum nesting depth")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        limit = _MAX_STRUCTURED_STRING_LENGTH if structured else _MAX_SAFE_STRING_LENGTH
        if len(value) > limit:
            raise ValueError(f"{path} exceeds maximum string length")
        if structured and redact_value(value) != value:
            raise ValueError(f"{path} contains sensitive dynamic data")
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        if len(value) > _MAX_STRUCTURED_ITEMS:
            raise ValueError(f"{path} exceeds maximum item count")
        for index, item in enumerate(value):
            _validate_json_value(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                structured=structured,
                budget=budget,
            )
        return
    if isinstance(value, dict):
        if len(value) > _MAX_STRUCTURED_ITEMS:
            raise ValueError(f"{path} exceeds maximum item count")
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} contains a non-string key")
            normalized = key.strip().lower().replace("-", "_")
            if normalized in _PROHIBITED_PAYLOAD_KEYS or _is_prohibited_payload_key(key):
                raise ValueError(f"{path} contains prohibited key: {key}")
            if structured and redact_value({key: item}) != {key: item}:
                raise ValueError(f"{path} contains sensitive dynamic data")
            _validate_json_value(
                item,
                path=f"{path}.{key}",
                depth=depth + 1,
                structured=structured,
                budget=budget,
            )
        return
    raise ValueError(f"{path} contains a non-JSON value: {type(value).__name__}")


def _validate_tool_scalar(value: Any, *, field_name: str, path: str) -> None:
    """按字段语义约束 Tool 摘要，禁止借合法键持久化任意正文。"""
    if field_name in _TOOL_INTEGER_FIELDS:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{path} must be a non-negative integer")
        return
    if field_name in _TOOL_BOOLEAN_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"{path} must be a boolean")
        return
    if field_name in _TOOL_ENUM_FIELDS:
        if value not in _TOOL_ENUM_FIELDS[field_name]:
            raise ValueError(f"{path} contains an unsupported value")
        return
    if field_name in {"start_time", "end_time"}:
        if not isinstance(value, str) or _ISO_TIMESTAMP.fullmatch(value) is None:
            raise ValueError(f"{path} must be an ISO-8601 timestamp")
        return
    if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{path} must be a bounded identifier")


def _validate_tool_object(
    value: dict[str, Any], *, allowed_keys: frozenset[str], path: str
) -> None:
    unknown = sorted(set(value) - allowed_keys)
    if unknown:
        raise ValueError(f"{path} keys are not allowed: {', '.join(unknown)}")
    _validate_json_value(value, path=path, structured=True)
    for field_name, field_value in value.items():
        field_path = f"{path}.{field_name}"
        if field_name in _TOOL_LIST_FIELDS:
            if not isinstance(field_value, list) or len(field_value) > 20:
                raise ValueError(f"{field_path} must be a bounded list")
            for index, item in enumerate(field_value):
                _validate_tool_scalar(
                    item, field_name="identifier", path=f"{field_path}[{index}]"
                )
            continue
        _validate_tool_scalar(field_value, field_name=field_name, path=field_path)


class RuntimeEvent(RuntimeModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    attempt_id: str
    sequence: int = Field(ge=1)
    event_type: RuntimeEventType | str
    phase: RuntimePhase | str | None = None
    actor_type: RuntimeActorType | str
    actor_name: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    tool_call_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    schema_version: int = Field(default=1, ge=1)
    safe_payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: RuntimeEventType | str):
        try:
            return RuntimeEventType(value)
        except ValueError:
            if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
                raise ValueError("event_type must be a bounded identifier") from None
            return value

    @field_validator("phase")
    @classmethod
    def validate_phase(cls, value: RuntimePhase | str | None):
        if value is None:
            return None
        try:
            return RuntimePhase(value)
        except ValueError:
            if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
                raise ValueError("phase must be a bounded identifier") from None
            return value

    @field_validator("actor_type")
    @classmethod
    def validate_actor_type(cls, value: RuntimeActorType | str):
        try:
            return RuntimeActorType(value)
        except ValueError:
            if not isinstance(value, str) or _SAFE_IDENTIFIER.fullmatch(value) is None:
                raise ValueError("actor_type must be a bounded identifier") from None
            return value

    @field_validator("safe_payload")
    @classmethod
    def validate_safe_payload(cls, value: dict[str, Any], info) -> dict[str, Any]:
        event_type = info.data.get("event_type")
        opaque = (
            not isinstance(event_type, RuntimeEventType)
            or info.data.get("schema_version") != 1
        )
        allowed = (
            _OPAQUE_EVENT_PAYLOAD_KEYS
            if opaque
            else _EVENT_PAYLOAD_KEYS.get(event_type, frozenset())
        )
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError(f"safe_payload keys are not allowed: {', '.join(unknown)}")
        sanitized = dict(value)
        if "message" in sanitized:
            if not isinstance(sanitized["message"], str):
                raise ValueError("safe_payload.message must be a string")
            sanitized["message"] = redact_text(sanitized["message"])[
                :_MAX_SAFE_STRING_LENGTH
            ]
        _validate_json_value(sanitized)
        if opaque:
            metadata = sanitized.get("metadata")
            if metadata is not None:
                if not isinstance(metadata, dict):
                    raise ValueError("safe_payload.metadata must be an object")
                _validate_json_value(
                    metadata,
                    path="safe_payload.metadata",
                    structured=True,
                )
            return sanitized
        if "normalized_inputs" in sanitized or "metadata" in sanitized:
            tool_name = sanitized.get("tool_name")
            if not isinstance(tool_name, str) or tool_name not in _TOOL_NORMALIZED_INPUT_KEYS:
                raise ValueError("safe_payload tool_name is required for structured tool data")
        if "normalized_inputs" in sanitized:
            normalized_inputs = sanitized["normalized_inputs"]
            if not isinstance(normalized_inputs, dict):
                raise ValueError("safe_payload.normalized_inputs must be an object")
            _validate_tool_object(
                normalized_inputs,
                allowed_keys=_TOOL_NORMALIZED_INPUT_KEYS[tool_name],
                path="safe_payload.normalized_inputs",
            )
        if "metadata" in sanitized:
            metadata = sanitized["metadata"]
            if not isinstance(metadata, dict):
                raise ValueError("safe_payload.metadata must be an object")
            _validate_tool_object(
                metadata,
                allowed_keys=_TOOL_METADATA_KEYS[tool_name],
                path="safe_payload.metadata",
            )
        return sanitized


class RuntimeCheckpoint(RuntimeModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    run_id: str
    attempt_id: str
    completed_phase: RuntimePhase
    event_sequence: int = Field(ge=1)
    state_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    projection_digest: str = Field(
        default="0" * 64, pattern=r"^[0-9a-f]{64}$"
    )
    resume_state: RuntimeResumeState
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: int = Field(default=1, ge=1)


class ReplayReport(RuntimeModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    replay_run_id: str
    source_run_id: str
    valid: bool
    validation_errors: list[str] = Field(default_factory=list)
    benchmark_evaluation: str = "not_applicable"
    external_call_count: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeDiffSection(RuntimeModel):
    left: Any = None
    right: Any = None
    changed: bool


class RuntimeRunDiff(RuntimeModel):
    run_id: str
    against_run_id: str
    sections: dict[str, RuntimeDiffSection] = Field(default_factory=dict)
