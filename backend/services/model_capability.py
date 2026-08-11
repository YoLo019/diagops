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
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import openai
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import canonicalize_endpoint, endpoint_id
from backend.diagnosis.openai_compatible_model import COMPATIBLE_ADAPTER_VERSION
from backend.safety.redaction import redact_text

CAPABILITY_SCHEMA_VERSION = "model-capability-v1"
CAPABILITY_API_MODE = "chat_completions"
# 能力清单是工件身份的一部分；任何改动都会改变 manifest 哈希。
CAPABILITY_MANIFEST = (
    "non_streaming_chat_completions",
    "tool_calls",
    "json_object_output",
    "token_usage",
    "bounded_response_deadline",
    "configured_parallelism",
)
DEFAULT_CERTIFICATION_DEADLINE_SECONDS = 30.0
DEFAULT_CERTIFICATION_PARALLELISM = 3
REQUIRED_CONTRACTS = (
    "chat_completions.non_streaming",
    "chat_completions.tool_calls",
    "chat_completions.json_object_output",
    "chat_completions.token_usage",
    "chat_completions.bounded_response_deadline",
    "chat_completions.configured_parallelism",
)


def capability_manifest_hash() -> str:
    canonical = json.dumps(
        list(CAPABILITY_MANIFEST), ensure_ascii=True, separators=(",", ":")
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
    endpoint_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_version: str = Field(min_length=1, max_length=64)
    openai_sdk_version: str = Field(min_length=1, max_length=32)
    agents_sdk_version: str = Field(min_length=1, max_length=32)
    tested_parallelism: int = Field(ge=1, le=16)
    capability_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_contracts: tuple[str, ...] = Field(min_length=len(REQUIRED_CONTRACTS))
    code_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
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
    canonical = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _artifact_dir(directory: Path, artifact_identity: tuple[str, str]) -> Path:
    endpoint, model = artifact_identity
    safe_model = "".join(
        char if char.isalnum() or char in "-._" else "_" for char in model
    )[:64]
    return directory / f"{endpoint[:16]}-{safe_model}"


def write_capability_artifact(
    directory: Path, artifact: ModelCapabilityArtifact
) -> Path:
    """计算并回填 artifact_hash 后原子写入；返回路径。"""
    sealed = artifact.model_copy(
        update={"artifact_hash": compute_artifact_hash(artifact)}
    )
    target_dir = _artifact_dir(directory, (sealed.endpoint_id, sealed.model))
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "result.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(sealed.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def read_capability_artifact(path: Path) -> ModelCapabilityArtifact:
    artifact = ModelCapabilityArtifact.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )
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


def _git_identity() -> tuple[str, bool]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        revision = result.stdout.strip()
        if result.returncode == 0 and revision:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return revision, bool(status.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40, True


def _code_revision() -> str:
    return _git_identity()[0]


def validate_capability_for_prediction(
    artifact: ModelCapabilityArtifact,
    *,
    provider: str,
    model: str,
    endpoint_id_value: str,
    expected_parallelism: int,
) -> None:
    """Admission gate for formal prediction; every identity is current-bound."""
    if artifact.result != "passed":
        raise ValueError("capability result is not passed")
    if artifact.provider != provider or artifact.model != model:
        raise ValueError("capability provider/model identity is stale")
    if artifact.endpoint_id != endpoint_id_value:
        raise ValueError("capability endpoint identity is stale")
    revision, dirty = _git_identity()
    if artifact.git_dirty or dirty or artifact.code_revision != revision:
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
    if set(observations) != set(CAPABILITY_MANIFEST) or not all(observations.values()):
        raise ValueError("capability observations are incomplete or failed")


async def _probe_capabilities(
    client: openai.AsyncOpenAI,
    *,
    model: str,
    parallelism: int,
    deadline_seconds: float,
) -> list[CapabilityObservation]:
    """只通过远端可观测行为探测能力；不记录 prompt 与响应正文。"""

    async def non_streaming() -> bool:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the word ok."}],
            max_tokens=8,
        )
        return bool(response.choices and response.choices[0].message)

    async def json_object() -> bool:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": "Reply with JSON: {\"ok\": true}"}
            ],
            response_format={"type": "json_object"},
            max_tokens=32,
        )
        content = response.choices[0].message.content if response.choices else None
        return isinstance(content, str) and isinstance(json.loads(content), dict)

    async def tool_calls() -> bool:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Read the logs for window now."}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_logs",
                        "description": "Read log evidence.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {"type": "string"},
                            },
                            "required": ["reason"],
                        },
                    },
                }
            ],
            tool_choice="auto",
            max_tokens=64,
        )
        if not response.choices:
            return False
        calls = response.choices[0].message.tool_calls or []
        return any(call.function and call.function.name == "read_logs" for call in calls)

    async def token_usage() -> bool:
        response = await client.chat.completions.create(
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
        return all(result is True for result in results)

    probes = (
        ("non_streaming_chat_completions", non_streaming),
        ("tool_calls", tool_calls),
        ("json_object_output", json_object),
        ("token_usage", token_usage),
        ("configured_parallelism", configured_parallelism),
    )
    observations: list[CapabilityObservation] = []
    for name, probe in probes:
        try:
            passed = await asyncio.wait_for(probe(), timeout=deadline_seconds)
            detail = None
        except TimeoutError:
            passed = False
            detail = "probe exceeded certification deadline"
        except Exception as exc:
            passed = False
            detail = redact_text(type(exc).__name__)
        observations.append(
            CapabilityObservation(capability=name, passed=bool(passed), detail=detail)
        )
    observations.append(
        CapabilityObservation(
            capability="bounded_response_deadline",
            passed=all(
                item.detail != "probe exceeded certification deadline"
                for item in observations
            ),
            detail=None,
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
        )
    finally:
        await client.close()
    openai_version, agents_version = _sdk_versions()
    code_revision, git_dirty = _git_identity()
    return ModelCapabilityArtifact(
        provider="openai_compatible",
        model=model,
        api_mode=CAPABILITY_API_MODE,
        endpoint_id=endpoint_id(canonical),
        adapter_version=COMPATIBLE_ADAPTER_VERSION,
        openai_sdk_version=openai_version,
        agents_sdk_version=agents_version,
        tested_parallelism=parallelism,
        capability_manifest_hash=capability_manifest_hash(),
        required_contracts=REQUIRED_CONTRACTS,
        code_revision=code_revision,
        git_dirty=git_dirty,
        execution_environment=current_execution_environment(),
        tested_at=datetime.now(UTC),
        result=(
            "passed" if all(item.passed for item in observations) else "failed"
        ),
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
        "--deadline-seconds", type=float, default=DEFAULT_CERTIFICATION_DEADLINE_SECONDS
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output/model_capability")
    )
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
            print(f"FAILED {observation.capability}: {observation.detail}")
    return 0 if artifact.result == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
