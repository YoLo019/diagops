"""model-capability-v1 工件与 live 认证（fake endpoint，不接触真实模型）。"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from backend.config.settings import endpoint_id
from backend.services.model_capability import (
    CAPABILITY_MANIFEST,
    REQUIRED_CONTRACTS,
    ModelCapabilityArtifact,
    capability_certification_status,
    capability_manifest_hash,
    certify_endpoint_async,
    compute_artifact_hash,
    current_execution_environment,
    main,
    read_capability_artifact,
    validate_capability_for_prediction,
    write_capability_artifact,
)

_CANONICAL_URL = "http://127.0.0.1:8000/v1"
_ENDPOINT_ID = endpoint_id(_CANONICAL_URL)
_TESTED_AT = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def _artifact(**overrides):
    values = {
        "provider": "openai_compatible",
        "model": "compat-model",
        "structured_output_transport": "native_json_schema",
        "endpoint_id": _ENDPOINT_ID,
        "adapter_version": "openai-compatible-adapter-v2",
        "openai_sdk_version": "1.0.0",
        "agents_sdk_version": "0.18.1",
        "tested_parallelism": 2,
        "capability_manifest_hash": capability_manifest_hash(),
        "required_contracts": REQUIRED_CONTRACTS,
        "code_revision": "a" * 40,
        "source_manifest_hash": "b" * 64,
        "execution_environment": current_execution_environment(),
        "tested_at": _TESTED_AT,
        "result": "passed",
        "observations": [],
    }
    values.update(overrides)
    return ModelCapabilityArtifact(**values)


def test_artifact_hash_excludes_its_own_field():
    artifact = _artifact()
    digest = compute_artifact_hash(artifact)
    sealed = artifact.model_copy(update={"artifact_hash": digest})

    assert compute_artifact_hash(sealed) == digest
    assert len(digest) == 64


def test_write_read_roundtrip_and_tamper_detection(tmp_path):
    artifact = _artifact()
    path = write_capability_artifact(tmp_path, artifact)

    restored = read_capability_artifact(path)
    assert restored.artifact_hash == compute_artifact_hash(artifact)

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["result"] = "failed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        read_capability_artifact(path)


def test_artifact_never_persists_url_key_or_bodies(tmp_path):
    artifact = _artifact()
    path = write_capability_artifact(tmp_path, artifact)

    text = path.read_text(encoding="utf-8")
    assert _CANONICAL_URL not in text
    assert "api_key" not in text
    assert "prompt" not in text
    assert "response_body" not in text


def test_status_requires_exact_tuple(tmp_path):
    write_capability_artifact(tmp_path, _artifact())

    assert (
        capability_certification_status(
            tmp_path,
            provider="openai_compatible",
            model="compat-model",
            endpoint_id_value=_ENDPOINT_ID,
        )
        == "passed"
    )
    assert (
        capability_certification_status(
            tmp_path,
            provider="openai_compatible",
            model="other-model",
            endpoint_id_value=_ENDPOINT_ID,
        )
        == "not_run"
    )
    assert (
        capability_certification_status(
            tmp_path,
            provider="openai_compatible",
            model="compat-model",
            endpoint_id_value=endpoint_id("https://other.example/v1"),
        )
        == "not_run"
    )


def test_latest_failed_overrides_earlier_passed(tmp_path):
    write_capability_artifact(tmp_path, _artifact())
    write_capability_artifact(
        tmp_path,
        _artifact(
            result="failed",
            tested_at=datetime(2026, 8, 7, 13, 0, tzinfo=UTC),
        ),
    )

    assert (
        capability_certification_status(
            tmp_path,
            provider="openai_compatible",
            model="compat-model",
            endpoint_id_value=_ENDPOINT_ID,
        )
        == "failed"
    )


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeToolCall:
    def __init__(self, name, arguments="{}", call_id="call-1"):
        self.id = call_id
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()

    def model_dump(self):
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5


_DEFAULT_USAGE = object()


class _FakeResponse:
    def __init__(self, payload, usage=_DEFAULT_USAGE):
        self.choices = [_FakeChoice(payload)]
        self.usage = _FakeUsage() if usage is _DEFAULT_USAGE else usage


class _FakeCompletions:
    """确定性 fake endpoint：按请求形状应答，全部探测在本地完成。"""

    def __init__(
        self,
        *,
        with_usage=True,
        with_tool_calls=True,
        supports_native_schema=True,
        supports_strict_output_tool=True,
    ):
        self._with_usage = with_usage
        self._with_tool_calls = with_tool_calls
        self._supports_native_schema = supports_native_schema
        self._supports_strict_output_tool = supports_strict_output_tool
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        response_format = kwargs.get("response_format")
        if response_format and response_format.get("type") == "json_schema":
            if not self._supports_native_schema:
                raise RuntimeError("native schema unsupported")
            return _FakeResponse(
                _FakeMessage(content='{"ok":true,"scope":null,"items":[]}')
            )
        if response_format:
            return _FakeResponse(_FakeMessage(content='{"ok": true}'))
        if kwargs.get("tools"):
            function_names = {
                tool["function"]["name"] for tool in kwargs["tools"]
            }
            if "submit_structured_output" in function_names:
                if not self._supports_strict_output_tool:
                    return _FakeResponse(_FakeMessage(tool_calls=None))
                is_follow_up = any(
                    message.get("role") == "tool" for message in kwargs["messages"]
                )
                if not is_follow_up:
                    return _FakeResponse(
                        _FakeMessage(
                            tool_calls=[
                                _FakeToolCall(
                                    "read_logs",
                                    '{"payload_json":"{\\"reason\\":\\"certify\\"}"}',
                                )
                            ]
                        )
                    )
                return _FakeResponse(
                    _FakeMessage(
                        tool_calls=[
                            _FakeToolCall(
                                "submit_structured_output",
                                '{"payload_json":"{\\"ok\\":true}"}',
                            )
                        ]
                    )
                )
            tool_calls = (
                [_FakeToolCall("read_logs")] if self._with_tool_calls else None
            )
            return _FakeResponse(_FakeMessage(tool_calls=tool_calls))
        usage = _FakeUsage() if self._with_usage else None
        return _FakeResponse(_FakeMessage(content="ok"), usage=usage)


class _FakeClient:
    def __init__(self, completions):
        self.chat = type("Chat", (), {"completions": completions})()
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.anyio
async def test_certify_passes_against_fake_endpoint():
    completions = _FakeCompletions()
    client = _FakeClient(completions)

    artifact = await certify_endpoint_async(
        base_url="HTTP://127.0.0.1:8000/v1/",
        model="compat-model",
        api_key="local-secret",
        client_factory=lambda: client,
    )

    assert artifact.result == "passed"
    assert artifact.endpoint_id == _ENDPOINT_ID
    assert artifact.api_mode == "chat_completions"
    assert {item.capability for item in artifact.observations} == set(
        CAPABILITY_MANIFEST
    )
    assert client.closed is True
    assert artifact.artifact_hash is None  # hash 只在写盘时回填
    assert artifact.structured_output_transport == "native_json_schema"
    assert any(
        request.get("response_format", {}).get("type") == "json_schema"
        and request["response_format"]["json_schema"]["strict"] is True
        and request.get("tools")
        for request in completions.requests
    )
    assert "json_object_output" not in CAPABILITY_MANIFEST
    assert {
        "native_json_schema_with_tools",
        "strict_output_tool",
    } <= set(CAPABILITY_MANIFEST)


@pytest.mark.anyio
async def test_certify_selects_strict_output_tool_when_native_schema_is_unavailable():
    completions = _FakeCompletions(supports_native_schema=False)

    artifact = await certify_endpoint_async(
        base_url=_CANONICAL_URL,
        model="compat-model",
        api_key="local-secret",
        client_factory=lambda: _FakeClient(completions),
    )

    assert artifact.result == "passed"
    assert artifact.structured_output_transport == "strict_output_tool"
    observations = {item.capability: item.passed for item in artifact.observations}
    assert observations["native_json_schema_with_tools"] is False
    assert observations["strict_output_tool"] is True
    strict_tool_request = next(
        request
        for request in completions.requests
        if request.get("tools")
        and any(
            tool["function"]["name"] == "submit_structured_output"
            for tool in request["tools"]
        )
    )
    function = next(
        tool["function"]
        for tool in strict_tool_request["tools"]
        if tool["function"]["name"] == "submit_structured_output"
    )
    assert strict_tool_request["tool_choice"] == "required"
    assert {tool["function"]["name"] for tool in strict_tool_request["tools"]} == {
        "read_logs",
        "submit_structured_output",
    }
    assert any(
        any(message.get("role") == "tool" for message in request["messages"])
        for request in completions.requests
        if request.get("tools")
        and any(
            tool["function"]["name"] == "submit_structured_output"
            for tool in request["tools"]
        )
    )
    assert function["strict"] is True
    assert function["parameters"]["additionalProperties"] is False
    assert set(function["parameters"]["properties"]) == {"payload_json"}


@pytest.mark.anyio
async def test_certify_fails_when_usage_or_tool_calls_missing():
    artifact = await certify_endpoint_async(
        base_url=_CANONICAL_URL,
        model="compat-model",
        api_key="local-secret",
        client_factory=lambda: _FakeClient(
            _FakeCompletions(with_usage=False, with_tool_calls=False)
        ),
    )

    assert artifact.result == "failed"
    failed = {item.capability for item in artifact.observations if not item.passed}
    assert "token_usage" in failed
    assert "tool_calls" in failed


def test_main_requires_env_key(monkeypatch, tmp_path):
    monkeypatch.delenv("DIAGOPS_AGENTS_API_KEY", raising=False)

    exit_code = main(
        [
            "--base-url",
            _CANONICAL_URL,
            "--model",
            "compat-model",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 2


def test_main_writes_artifact_without_persisting_key(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_AGENTS_API_KEY", "local-secret")
    artifact = _artifact()
    import backend.services.model_capability as module

    async def fake_certify(**kwargs):
        assert kwargs["api_key"] == "local-secret"
        return artifact

    monkeypatch.setattr(module, "certify_endpoint_async", fake_certify)

    exit_code = main(
        [
            "--base-url",
            _CANONICAL_URL,
            "--model",
            "compat-model",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    persisted = next(tmp_path.glob("*/result.json")).read_text(encoding="utf-8")
    assert "local-secret" not in persisted


def test_prediction_admission_rejects_stale_revision_and_insufficient_parallelism(
    monkeypatch,
):
    artifact = _artifact(code_revision="0" * 40, tested_parallelism=1)
    with pytest.raises(ValueError, match="code revision|parallelism|stale"):
        validate_capability_for_prediction(
            artifact,
            provider="openai_compatible",
            model="compat-model",
            endpoint_id_value=_ENDPOINT_ID,
            expected_parallelism=3,
            repository_root=Path(__file__).resolve().parents[2],
        )


def test_git_identity_is_explicit_and_independent_of_process_cwd(tmp_path, monkeypatch):
    from backend.services.model_capability import _git_identity

    repository_root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(tmp_path)
    revision, dirty = _git_identity(repository_root)
    assert len(revision) == 40
    assert isinstance(dirty, bool)


def test_packaged_source_identity_without_git_rejects_tampering(tmp_path):
    package_root = tmp_path / "package"
    (package_root / "backend").mkdir(parents=True)
    (package_root / "backend" / "worker.py").write_text(
        "print('trusted')\n",
        encoding="utf-8",
    )

    from backend.services.source_identity import (
        resolve_source_identity,
        write_source_manifest,
    )

    write_source_manifest(package_root)
    identity = resolve_source_identity(package_root)
    assert len(identity.revision) == 40
    assert len(identity.manifest_hash) == 64
    assert identity.git_dirty is False

    (package_root / "backend" / "worker.py").write_text(
        "print('tampered')\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest|digest|tamper"):
        resolve_source_identity(package_root)
