"""model-capability-v1 能力认证工件与 live 认证命令（spec 7.8/§12/R26）。

与既有 provider reliability 工件完全分离：本工件绑定 canonical endpoint 哈希
（endpoint_id）、provider/model/API mode、能力观测、代码与配置身份，并使用
排除自身 hash 字段的 canonical JSON 哈希。任何环节都不持久化 URL、凭证、
prompt、证据或响应正文。live Run 只有精确冻结 tuple 具备 passed 工件时才可
使用 generic endpoint；formal scoring 无匹配 passed 工件不得启动。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import openai
from agents import AgentOutputSchema
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import canonicalize_endpoint, endpoint_id
from backend.diagnosis import v11_runtime as _v11_runtime
from backend.diagnosis.openai_compatible_model import (
    COMPATIBLE_ADAPTER_VERSION,
    compatible_extra_body,
)
from backend.diagnosis.v11_runtime import (
    V11SingleControlOutput,
    _strict_output_tool,
    v11_workflow_output_schemas,
)
from backend.safety.redaction import redact_text
from backend.services.source_identity import resolve_source_identity
from backend.services.v11_canary import run_v11_local_workflow_canary

CAPABILITY_SCHEMA_VERSION = "model-capability-v1"
CAPABILITY_API_MODE = "chat_completions"
# 能力清单是工件身份的一部分；任何改动都会改变 manifest 哈希。
CAPABILITY_MANIFEST = (
    "non_streaming_chat_completions",
    "tool_calls",
    "native_json_schema_with_tools",
    "strict_output_tool",
    "token_usage",
    "bounded_response_deadline",
    "configured_parallelism",
    "v11_remote_role_schema_capability",
    "v11_local_workflow_correctness",
)
DEFAULT_CERTIFICATION_DEADLINE_SECONDS = 30.0
DEFAULT_CERTIFICATION_PARALLELISM = 3
REQUIRED_CONTRACTS = (
    "chat_completions.non_streaming",
    "chat_completions.tool_calls",
    "chat_completions.strict_structured_output",
    "chat_completions.token_usage",
    "chat_completions.bounded_response_deadline",
    "chat_completions.configured_parallelism",
    "v11.remote_role_schema_capability",
    "v11.local_workflow_correctness",
)
STRUCTURED_OUTPUT_TRANSPORTS = ("native_json_schema", "strict_output_tool")
_NATIVE_PROBE_RESULT = {
    "action": "inconclusive",
    "summary": "Synthetic schema probe",
    "evidence_ids": [],
    "stop_reason": "schema_probe",
    "candidates": [],
}

# 兼容测试/调用方的只读别名；业务逻辑始终动态读取 runtime 模块中的唯一清单。
V11_WORKFLOW_OUTPUT_TYPES = _v11_runtime.V11_WORKFLOW_OUTPUT_TYPES
V11_PRODUCTION_OUTPUT_TYPES = _v11_runtime.V11_PRODUCTION_OUTPUT_TYPES
# 旧测试/调用方可继续对 V11Runtime 类做故障注入；实际实现仍来自 diagnosis
# 模块，认证服务不维护第二个 Runtime 类型。
V11Runtime = _v11_runtime.V11Runtime


async def _synthetic_v11_workflow_canary() -> bool:
    """兼容旧认证调用面；实现委托给生产入口。"""
    return await run_v11_local_workflow_canary()


def _native_production_schema() -> dict[str, object]:
    # 保留既有单上下文 probe 名称，但 schema 由唯一 workflow 清单提供。
    return _workflow_production_schemas()[V11SingleControlOutput.__name__]


def _workflow_production_schemas() -> dict[str, dict[str, object]]:
    """从 V11 唯一 live output-type 清单派生 manifest schema。"""
    return v11_workflow_output_schemas()


def _role_response_format(output_type: type[BaseModel]) -> dict[str, object]:
    """构造与 compatible adapter 相同的 native strict JSON 请求。"""
    schema = _workflow_production_schemas()[output_type.__name__]
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "final_output",
            "strict": True,
            "schema": schema,
        },
    }


def _role_output_tool(output_type: type[BaseModel]) -> dict[str, object]:
    """构造与 V11 `_strict_output_tool` 相同的 wire tool。"""
    tool = _strict_output_tool(output_type)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "strict": tool.strict_json_schema,
            "parameters": tool.params_json_schema,
        },
    }


def _role_probe_payload(output_type: type[BaseModel]) -> dict[str, object]:
    """返回不含业务身份的最小合法 role payload。"""
    if output_type is V11SingleControlOutput:
        return dict(_NATIVE_PROBE_RESULT)
    if output_type.__name__ == "LeadPlanningCompactOutput":
        return {
            "decision": {
                "action": "investigate",
                "summary": "Synthetic planning schema probe.",
                "selected_skills": [],
            },
            "tasks": [],
        }
    if output_type.__name__ == "LeadPlanningOutput":
        return {
            "decision": {
                "action": "investigate",
                "summary": "Synthetic planning schema probe.",
                "task_ids": ["probe-task"],
                "candidate_ids": [],
                "evidence_ids": [],
                "selected_skills": [],
                "stop_reason": None,
            },
            "tasks": [
                {
                    "id": "probe-task",
                    "title": "Inspect synthetic signal",
                    "description": "Inspect bounded synthetic evidence.",
                    "analysis_round": 1,
                    "tool_names": [],
                    "strategy": None,
                    "evidence_scope": None,
                    "expected_discriminator": None,
                    "information_gap": "synthetic_gap",
                }
            ],
        }
    if output_type.__name__ in {"InvestigatorCandidateOutput", "InvestigatorOutput"}:
        payload: dict[str, object] = {"candidates": []}
        if output_type.__name__ == "InvestigatorOutput":
            payload.update({"summary": "", "findings": []})
        return payload
    if output_type.__name__ in {"CriticCompactOutput", "CriticOutput"}:
        payload = {"assessments": []}
        payload["final_decision"] = _role_probe_payload(_v11_runtime.FinalDecisionDraft)
        fields = output_type.model_fields
        if "tasks" in fields:
            payload["tasks"] = []
        if "summary" in fields:
            payload["summary"] = "Synthetic critic schema probe."
        return payload
    if output_type.__name__ == "FinalDecisionDraft":
        return {
            "action": "inconclusive",
            "candidate_refs": [],
            "evidence_ids": [],
            "summary": "Synthetic adjudication schema probe.",
            "stop_reason": "schema_probe",
        }
    if output_type.__name__ == "LeadAdjudicationOutput":
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "Synthetic adjudication schema probe.",
                "task_ids": [],
                "candidate_ids": [],
                "evidence_ids": [],
                "selected_skills": [],
                "stop_reason": "schema_probe",
            }
        }
    raise ValueError("unknown V11 workflow output type")


def _native_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "final_output",
            "strict": True,
            "schema": _native_production_schema(),
        },
    }


_STRICT_OUTPUT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_structured_output",
        "description": "Submit the structured capability result.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"payload_json": {"type": "string"}},
            "required": ["payload_json"],
            "additionalProperties": False,
        },
    },
}
_STRICT_EVIDENCE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_logs",
        "description": "Read log evidence. Encode arguments in payload_json.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"payload_json": {"type": "string"}},
            "required": ["payload_json"],
            "additionalProperties": False,
        },
    },
}
_READ_LOGS_TOOL = {
    "type": "function",
    "function": {
        "name": "read_logs",
        "description": "Read log evidence.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
}


def capability_manifest_hash() -> str:
    canonical = json.dumps(
        {
            "capabilities": list(CAPABILITY_MANIFEST),
            "native_output_schema": _native_production_schema(),
            "workflow_schemas": _workflow_production_schemas(),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class CapabilityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str = Field(min_length=1, max_length=64)
    passed: bool
    detail: str | None = Field(default=None, max_length=128)


class CapabilityExecutionEnvironment(BaseModel):
    """非敏感执行环境身份；不包含主机名、路径、凭据或响应内容。"""

    model_config = ConfigDict(extra="forbid")

    implementation: str = Field(min_length=1, max_length=32)
    python_version: str = Field(min_length=1, max_length=32)
    platform_system: str = Field(min_length=1, max_length=32)
    platform_machine: str = Field(min_length=1, max_length=64)
    platform_release: str = Field(min_length=1, max_length=128)


class ModelCapabilityArtifact(BaseModel):
    """model-capability-v1；artifact_hash 字段不参与自身哈希 preimage。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["model-capability-v1"] = CAPABILITY_SCHEMA_VERSION
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    api_mode: Literal["chat_completions"] = CAPABILITY_API_MODE
    structured_output_transport: Literal["native_json_schema", "strict_output_tool"]
    endpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_version: str = Field(min_length=1, max_length=64)
    openai_sdk_version: str = Field(min_length=1, max_length=32)
    agents_sdk_version: str = Field(min_length=1, max_length=32)
    tested_parallelism: int = Field(ge=1, le=16)
    capability_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_contracts: tuple[str, ...] = Field(min_length=len(REQUIRED_CONTRACTS))
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    git_dirty: bool = False
    execution_environment: CapabilityExecutionEnvironment
    tested_at: datetime
    result: Literal["passed", "failed"]
    observations: list[CapabilityObservation] = Field(default_factory=list, max_length=32)
    artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


def compute_artifact_hash(artifact: ModelCapabilityArtifact) -> str:
    """canonical JSON（sorted keys、紧凑分隔、无 NaN、省略 artifact_hash）。"""
    payload = artifact.model_dump(mode="json")
    payload.pop("artifact_hash", None)
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _artifact_dir(directory: Path, artifact_identity: tuple[str, str]) -> Path:
    endpoint, model = artifact_identity
    safe_model = "".join(char if char.isalnum() or char in "-._" else "_" for char in model)[:64]
    return directory / f"{endpoint[:16]}-{safe_model}"


def write_capability_artifact(directory: Path, artifact: ModelCapabilityArtifact) -> Path:
    """计算并回填 artifact_hash 后原子写入；返回路径。"""
    sealed = artifact.model_copy(update={"artifact_hash": compute_artifact_hash(artifact)})
    target_dir = _artifact_dir(directory, (sealed.endpoint_id, sealed.model))
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "result.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(sealed.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def read_capability_artifact(path: Path) -> ModelCapabilityArtifact:
    artifact = ModelCapabilityArtifact.model_validate(json.loads(path.read_text(encoding="utf-8")))
    if artifact.artifact_hash is None:
        raise ValueError("capability artifact is missing its hash")
    if compute_artifact_hash(artifact) != artifact.artifact_hash:
        raise ValueError("capability artifact hash mismatch")
    return artifact


def latest_capability_artifact(
    directory: Path,
    *,
    provider: str,
    model: str,
    endpoint_id_value: str,
    api_mode: str = CAPABILITY_API_MODE,
) -> ModelCapabilityArtifact | None:
    """精确 tuple（provider/model/endpoint/api mode）的最新有效认证工件。"""
    matching: list[ModelCapabilityArtifact] = []
    if directory.exists():
        for path in directory.glob("*/result.json"):
            try:
                artifact = read_capability_artifact(path)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if (
                artifact.provider == provider
                and artifact.model == model
                and artifact.endpoint_id == endpoint_id_value
                and artifact.api_mode == api_mode
            ):
                matching.append(artifact)
    if not matching:
        return None
    return max(matching, key=lambda item: item.tested_at)


def capability_certification_status(
    directory: Path,
    *,
    provider: str,
    model: str,
    endpoint_id_value: str,
    api_mode: str = CAPABILITY_API_MODE,
) -> Literal["passed", "failed", "not_run"]:
    """精确 tuple 的最新有效认证状态；reliability 工件永不能满足本状态。"""
    artifact = latest_capability_artifact(
        directory,
        provider=provider,
        model=model,
        endpoint_id_value=endpoint_id_value,
        api_mode=api_mode,
    )
    if artifact is None:
        return "not_run"
    return artifact.result


def _sdk_versions() -> tuple[str, str]:
    try:
        agents_version = importlib.metadata.version("openai-agents")
    except importlib.metadata.PackageNotFoundError:
        agents_version = "unknown"
    return openai.__version__, agents_version


def current_execution_environment() -> CapabilityExecutionEnvironment:
    return CapabilityExecutionEnvironment(
        implementation=sys.implementation.name,
        python_version=platform.python_version(),
        platform_system=platform.system(),
        platform_machine=platform.machine(),
        platform_release=platform.release(),
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_identity(repository_root: Path) -> tuple[str, bool]:
    identity = resolve_source_identity(repository_root)
    return identity.revision, identity.git_dirty


def _code_revision(repository_root: Path) -> str:
    return _git_identity(repository_root)[0]


def validate_capability_for_prediction(
    artifact: ModelCapabilityArtifact,
    *,
    provider: str,
    model: str,
    endpoint_id_value: str,
    expected_parallelism: int,
    repository_root: Path,
    require_clean_source: bool = True,
) -> None:
    """Admission gate for formal prediction; every identity is current-bound."""
    if artifact.result != "passed":
        raise ValueError("capability result is not passed")
    if artifact.provider != provider or artifact.model != model:
        raise ValueError("capability provider/model identity is stale")
    if artifact.endpoint_id != endpoint_id_value:
        raise ValueError("capability endpoint identity is stale")
    source_identity = resolve_source_identity(repository_root)
    if (
        (require_clean_source and (artifact.git_dirty or source_identity.git_dirty))
        or artifact.git_dirty != source_identity.git_dirty
        or artifact.code_revision != source_identity.revision
        or artifact.source_manifest_hash != source_identity.manifest_hash
    ):
        raise ValueError("capability code revision is stale or dirty")
    openai_version, agents_version = _sdk_versions()
    if artifact.adapter_version != COMPATIBLE_ADAPTER_VERSION:
        raise ValueError("capability adapter version is stale")
    if (artifact.openai_sdk_version, artifact.agents_sdk_version) != (
        openai_version,
        agents_version,
    ):
        raise ValueError("capability SDK versions are stale")
    if artifact.capability_manifest_hash != capability_manifest_hash():
        raise ValueError("capability manifest is stale")
    if artifact.required_contracts != REQUIRED_CONTRACTS:
        raise ValueError("capability required contracts are stale")
    if artifact.tested_parallelism < expected_parallelism:
        raise ValueError("capability tested parallelism is insufficient")
    if artifact.execution_environment != current_execution_environment():
        raise ValueError("capability execution environment is stale")
    observations = {item.capability: item.passed for item in artifact.observations}
    if set(observations) != set(CAPABILITY_MANIFEST):
        raise ValueError("capability observations are incomplete or failed")
    common = set(CAPABILITY_MANIFEST) - {
        "native_json_schema_with_tools",
        "strict_output_tool",
    }
    selected_observation = {
        "native_json_schema": "native_json_schema_with_tools",
        "strict_output_tool": "strict_output_tool",
    }[artifact.structured_output_transport]
    if not all(observations[name] for name in common) or not observations[selected_observation]:
        raise ValueError("capability observations are incomplete or failed")


async def _probe_capabilities(
    client: openai.AsyncOpenAI,
    *,
    model: str,
    parallelism: int,
    deadline_seconds: float,
    base_url: str = "",
) -> list[CapabilityObservation]:
    """只通过远端可观测行为探测能力；不记录 prompt 与响应正文。"""
    probe_results: dict[str, bool] = {}
    probe_details: dict[str, str] = {}
    timed_out: set[str] = set()

    async def request(**kwargs):
        # 截止时间约束每个远端请求，不包含角色探测等待并发槽位的时间。
        if extra_body := compatible_extra_body(base_url):
            kwargs["extra_body"] = extra_body
        return await asyncio.wait_for(
            client.chat.completions.create(**kwargs), timeout=deadline_seconds
        )

    async def non_streaming() -> bool:
        response = await request(
            model=model,
            messages=[{"role": "user", "content": "Reply with the word ok."}],
            max_tokens=8,
        )
        return bool(response.choices and response.choices[0].message)

    async def native_json_schema_with_tools() -> bool:
        response = await request(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Do not call read_logs. Return exactly this JSON result: "
                        + json.dumps(_NATIVE_PROBE_RESULT, ensure_ascii=True, sort_keys=True)
                    ),
                }
            ],
            response_format=_native_response_format(),
            tools=[_READ_LOGS_TOOL],
            tool_choice="auto",
            max_tokens=256,
        )
        content = response.choices[0].message.content if response.choices else None
        if not isinstance(content, str):
            return False
        parsed = AgentOutputSchema(V11SingleControlOutput).validate_json(content)
        return parsed.model_dump(mode="json") == _NATIVE_PROBE_RESULT

    async def strict_output_tool() -> bool:
        first = await request(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "First call read_logs with payload_json containing "
                        '{"reason": "certify"}. Do not submit the final result yet.'
                    ),
                }
            ],
            tools=[_STRICT_EVIDENCE_TOOL, _STRICT_OUTPUT_TOOL],
            tool_choice="required",
            max_tokens=64,
        )
        if not first.choices:
            return False
        first_message = first.choices[0].message
        evidence_calls = [
            call
            for call in (first_message.tool_calls or [])
            if call.function and call.function.name == "read_logs"
        ]
        if not evidence_calls or json.loads(
            json.loads(evidence_calls[0].function.arguments)["payload_json"]
        ) != {"reason": "certify"}:
            return False
        messages = [
            {
                "role": "user",
                "content": (
                    "First call read_logs with payload_json containing "
                    '{"reason": "certify"}. Do not submit the final result yet.'
                ),
            },
            {
                "role": "assistant",
                "content": first_message.content,
                "tool_calls": [call.model_dump() for call in evidence_calls],
            },
            *(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "bounded log evidence",
                }
                for call in evidence_calls
            ),
            {
                "role": "user",
                "content": (
                    "Now call submit_structured_output with payload_json containing "
                    'the JSON object {"ok": true}.'
                ),
            },
        ]
        second = await request(
            model=model,
            messages=messages,
            tools=[_STRICT_EVIDENCE_TOOL, _STRICT_OUTPUT_TOOL],
            tool_choice="required",
            max_tokens=64,
        )
        if not second.choices:
            return False
        final_calls = second.choices[0].message.tool_calls or []
        return any(
            call.function
            and call.function.name == "submit_structured_output"
            and json.loads(json.loads(call.function.arguments)["payload_json"]) == {"ok": True}
            for call in final_calls
        )

    async def remote_role_schema_capability() -> bool:
        """探测实际 live workflow 每个 role schema 的选定 transport。"""
        transport = (
            "native_json_schema"
            if probe_results.get("native_json_schema_with_tools")
            else "strict_output_tool"
            if probe_results.get("strict_output_tool")
            else None
        )
        if transport is None:
            return False
        role_gate = asyncio.Semaphore(max(1, parallelism))

        async def probe_role(output_type: type[BaseModel]) -> bool:
            # 所有角色共用端点并发上限；排队不占单次远端请求时限。
            async with role_gate:
                return await _probe_role(output_type)

        async def _probe_role(output_type: type[BaseModel]) -> bool:
            payload = _role_probe_payload(output_type)
            # 先用同一 output type 验证 probe 自身，避免新增 required 字段后
            # 探测器仍发送一个看似成功但无法被正式 Runtime 解析的样本。
            try:
                AgentOutputSchema(output_type, strict_json_schema=True).validate_json(
                    json.dumps(payload, ensure_ascii=True)
                )
            except (TypeError, ValueError):
                return False
            request_args: dict[str, object] = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Return exactly this JSON for the requested workflow role: "
                            + json.dumps(payload, ensure_ascii=True, sort_keys=True)
                        ),
                    }
                ],
                "max_tokens": 768,
            }
            if transport == "native_json_schema":
                request_args["response_format"] = _role_response_format(output_type)
            else:
                request_args["tools"] = [_role_output_tool(output_type)]
                request_args["tool_choice"] = "required"
            response = await request(**request_args)
            if not response.choices:
                return False
            message = response.choices[0].message
            if transport == "native_json_schema":
                raw = message.content
            else:
                call = next(
                    (
                        item
                        for item in (message.tool_calls or [])
                        if item.function and item.function.name == "submit_structured_output"
                    ),
                    None,
                )
                if call is None:
                    return False
                envelope = json.loads(call.function.arguments)
                if (
                    not isinstance(envelope, dict)
                    or set(envelope) != {"payload_json"}
                    or not isinstance(envelope["payload_json"], str)
                ):
                    return False
                raw = envelope.get("payload_json")
            if not isinstance(raw, str):
                return False
            # Parse with the same schema validator used by V11 runtime.  This is
            # deliberately after transport extraction so an endpoint that accepts
            # the wire shape but emits malformed role output still fails closed.
            AgentOutputSchema(output_type).validate_json(raw)
            return True

        results = await asyncio.gather(
            *(probe_role(output_type) for output_type in _v11_runtime.V11_WORKFLOW_OUTPUT_TYPES),
            return_exceptions=True,
        )
        failures = [
            f"{output_type.__name__}: "
            + (type(result).__name__ if isinstance(result, BaseException) else "invalid output")
            for output_type, result in zip(
                _v11_runtime.V11_WORKFLOW_OUTPUT_TYPES, results, strict=True
            )
            if result is not True
        ]
        if failures:
            probe_details["v11_remote_role_schema_capability"] = "; ".join(failures)[:128]
        if any(isinstance(result, (TimeoutError, openai.APITimeoutError)) for result in results):
            timed_out.add("v11_remote_role_schema_capability")
        return all(result is True for result in results)

    async def local_workflow_correctness() -> bool:
        """运行共享生产 phase/Store canary；不读取评估数据或标签。"""
        return await _synthetic_v11_workflow_canary()

    async def tool_calls() -> bool:
        response = await request(
            model=model,
            messages=[{"role": "user", "content": "Read the logs for window now."}],
            tools=[_READ_LOGS_TOOL],
            tool_choice="auto",
            max_tokens=64,
        )
        if not response.choices:
            return False
        calls = response.choices[0].message.tool_calls or []
        return any(call.function and call.function.name == "read_logs" for call in calls)

    async def token_usage() -> bool:
        response = await request(
            model=model,
            messages=[{"role": "user", "content": "Reply with the word ok."}],
            max_tokens=8,
        )
        usage = response.usage
        return (
            usage is not None
            and isinstance(usage.prompt_tokens, int)
            and isinstance(usage.completion_tokens, int)
        )

    async def configured_parallelism() -> bool:
        results = await asyncio.gather(
            *(non_streaming() for _ in range(parallelism)),
            return_exceptions=True,
        )
        if any(isinstance(result, (TimeoutError, openai.APITimeoutError)) for result in results):
            timed_out.add("configured_parallelism")
        return all(result is True for result in results)

    probes = (
        ("non_streaming_chat_completions", non_streaming),
        ("tool_calls", tool_calls),
        ("native_json_schema_with_tools", native_json_schema_with_tools),
        ("strict_output_tool", strict_output_tool),
        ("token_usage", token_usage),
        ("configured_parallelism", configured_parallelism),
        ("v11_remote_role_schema_capability", remote_role_schema_capability),
        ("v11_local_workflow_correctness", local_workflow_correctness),
    )
    observations: list[CapabilityObservation] = []
    for name, probe in probes:
        try:
            # 复合探测含多个有界请求；外层采用串行最坏时长，避免批次被误截断。
            request_count = (
                len(_v11_runtime.V11_WORKFLOW_OUTPUT_TYPES)
                if name == "v11_remote_role_schema_capability"
                else 2 if name == "strict_output_tool" else 1
            )
            passed = await asyncio.wait_for(probe(), timeout=deadline_seconds * request_count)
            detail = probe_details.get(name)
        except (TimeoutError, openai.APITimeoutError):
            passed = False
            detail = "probe exceeded certification deadline"
            timed_out.add(name)
        except Exception as exc:
            passed = False
            detail = redact_text(type(exc).__name__)
        observations.append(
            CapabilityObservation(capability=name, passed=bool(passed), detail=detail)
        )
        probe_results[name] = bool(passed)
    selected = (
        "native_json_schema_with_tools"
        if probe_results["native_json_schema_with_tools"]
        else "strict_output_tool"
    )
    required_timeouts = timed_out - (
        {"native_json_schema_with_tools", "strict_output_tool"} - {selected}
    )
    observations.append(
        CapabilityObservation(
            capability="bounded_response_deadline",
            passed=not required_timeouts,
            detail=("timed out: " + ", ".join(sorted(required_timeouts)))[:128]
            if required_timeouts else None,
        )
    )
    return observations


async def certify_endpoint_async(
    *,
    base_url: str,
    model: str,
    api_key: str,
    parallelism: int = DEFAULT_CERTIFICATION_PARALLELISM,
    deadline_seconds: float = DEFAULT_CERTIFICATION_DEADLINE_SECONDS,
    client_factory=None,
    repository_root: Path | None = None,
) -> ModelCapabilityArtifact:
    """对真实 compatible endpoint 运行 live 认证；只证明协议能力不证明准确性。"""
    canonical = canonicalize_endpoint(base_url)
    client = (
        client_factory()
        if client_factory is not None
        else openai.AsyncOpenAI(
            api_key=api_key,
            base_url=canonical,
            timeout=deadline_seconds,
            max_retries=0,
        )
    )
    try:
        observations = await _probe_capabilities(
            client,
            model=model,
            parallelism=parallelism,
            deadline_seconds=deadline_seconds,
            base_url=canonical,
        )
    finally:
        await client.close()
    openai_version, agents_version = _sdk_versions()
    source_identity = resolve_source_identity(
        _repository_root() if repository_root is None else repository_root
    )
    observation_status = {item.capability: item.passed for item in observations}
    transport = (
        "native_json_schema"
        if observation_status["native_json_schema_with_tools"]
        else "strict_output_tool"
    )
    common = set(CAPABILITY_MANIFEST) - {
        "native_json_schema_with_tools",
        "strict_output_tool",
    }
    passed = (
        all(observation_status[name] for name in common)
        and observation_status[
            {
                "native_json_schema": "native_json_schema_with_tools",
                "strict_output_tool": "strict_output_tool",
            }[transport]
        ]
    )
    return ModelCapabilityArtifact(
        provider="openai_compatible",
        model=model,
        api_mode=CAPABILITY_API_MODE,
        structured_output_transport=transport,
        endpoint_id=endpoint_id(canonical),
        adapter_version=COMPATIBLE_ADAPTER_VERSION,
        openai_sdk_version=openai_version,
        agents_sdk_version=agents_version,
        tested_parallelism=parallelism,
        capability_manifest_hash=capability_manifest_hash(),
        required_contracts=REQUIRED_CONTRACTS,
        code_revision=source_identity.revision,
        source_manifest_hash=source_identity.manifest_hash,
        git_dirty=source_identity.git_dirty,
        execution_environment=current_execution_environment(),
        tested_at=datetime.now(UTC),
        result="passed" if passed else "failed",
        observations=observations,
    )


def main(argv: list[str] | None = None) -> int:
    """live 认证命令：凭证只来自 DIAGOPS_AGENTS_API_KEY，永不入工件/日志。"""
    parser = argparse.ArgumentParser(
        description="Certify an OpenAI-compatible Chat Completions endpoint"
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--parallelism", type=int, default=DEFAULT_CERTIFICATION_PARALLELISM)
    parser.add_argument(
        "--deadline-seconds", type=float, default=DEFAULT_CERTIFICATION_DEADLINE_SECONDS,
        help="Deadline per remote request; compound probes have separate bounded batch deadlines",
    )
    parser.add_argument("--output-dir", type=Path, default=Path("output/model_capability"))
    args = parser.parse_args(argv)
    api_key = os.environ.get("DIAGOPS_AGENTS_API_KEY", "").strip()
    if not api_key:
        print("DIAGOPS_AGENTS_API_KEY is required for live certification")
        return 2
    artifact = asyncio.run(
        certify_endpoint_async(
            base_url=args.base_url,
            model=args.model,
            api_key=api_key,
            parallelism=args.parallelism,
            deadline_seconds=args.deadline_seconds,
        )
    )
    path = write_capability_artifact(args.output_dir, artifact)
    print(
        f"model capability: result={artifact.result} "
        f"endpoint_id={artifact.endpoint_id[:16]} artifact={path}"
    )
    for observation in artifact.observations:
        if not observation.passed:
            label = "UNAVAILABLE" if artifact.result == "passed" else "FAILED"
            print(f"{label} {observation.capability}: {observation.detail}")
    return 0 if artifact.result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
